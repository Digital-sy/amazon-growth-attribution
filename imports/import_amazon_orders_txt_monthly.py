#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将本地 Amazon All Orders TXT 月累计文件导入 ods_amz_all_orders_report。

文件名兼容：
- RKZ2607.txt
- RKZ2607-.txt
- RKZ2607-20260727.txt

命名解析：
- RKZ -> RKZ-US
- 26  -> 2026
- 07  -> 07月

安全策略：
- 默认仅预检，不写数据库。
- 正式写入必须同时指定 --apply --replace-month。
- 正式写入在一个事务中删除目标店铺目标月份旧数据，再写入整份月累计文件。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

try:
    import pymysql
except ImportError as exc:
    print(f"缺少依赖：{exc}", file=sys.stderr)
    print("请安装：pip install PyMySQL", file=sys.stderr)
    raise SystemExit(2)


STORE_CODE_MAP = {
    "JQ": "JQ-US",
    "MT": "MT-US",
    "RKZ": "RKZ-US",
    "SY": "SY-US",
}

SOURCE_ALIASES = {
    "purchase_date_utc": ("purchase_date_utc", "purchase_date"),
    "last_updated_date_utc": ("last_updated_date_utc", "last_updated_date"),
    "amazon_order_id": ("amazon_order_id",),
    "merchant_order_id": ("merchant_order_id",),
    "order_status": ("order_status",),
    "fulfillment_channel": ("fulfillment_channel",),
    "sales_channel": ("sales_channel",),
    "order_channel": ("order_channel",),
    "url": ("url",),
    "ship_service_level": ("ship_service_level",),
    "product_name": ("product_name",),
    "sku": ("sku", "seller_sku", "msku"),
    "asin": ("asin",),
    "item_status": ("item_status",),
    "quantity": ("quantity",),
    "currency": ("currency",),
    "item_price": ("item_price",),
    "item_tax": ("item_tax",),
    "shipping_price": ("shipping_price",),
    "shipping_tax": ("shipping_tax",),
    "gift_wrap_price": ("gift_wrap_price",),
    "gift_wrap_tax": ("gift_wrap_tax",),
    "item_promotion_discount": ("item_promotion_discount",),
    "ship_promotion_discount": ("ship_promotion_discount",),
    "ship_city": ("ship_city",),
    "ship_state": ("ship_state",),
    "ship_postal_code": ("ship_postal_code",),
    "ship_country": ("ship_country",),
    "promotion_ids": ("promotion_ids",),
    "is_business_order": ("is_business_order",),
    "purchase_order_number": ("purchase_order_number",),
    "price_designation": ("price_designation",),
    "fulfilled_by": ("fulfilled_by",),
    "buyer_company_name": ("buyer_company_name",),
    "order_item_id": ("order_item_id",),
}

REQUIRED_BUSINESS_FIELDS = (
    "store_name",
    "purchase_date_utc",
    "amazon_order_id",
    "sku",
    "quantity",
    "currency",
    "item_price",
)


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_name(value: str) -> str:
    value = clean(value).lstrip("\ufeff")
    value = value.replace("-", "_").replace(" ", "_").replace("/", "_")
    value = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value.lower()


def parse_filename(path: Path) -> tuple[str, str]:
    match = re.match(r"^(JQ|MT|RKZ|SY)(\d{2})(\d{2})(?:-|$)", path.stem, re.I)
    if not match:
        raise ValueError(
            f"无法从文件名解析店铺和月份：{path.name}。"
            "期望格式如 RKZ2607.txt、RKZ2607-.txt 或 RKZ2607-说明.txt"
        )
    code, yy, mm = match.groups()
    month_num = int(mm)
    if not 1 <= month_num <= 12:
        raise ValueError(f"文件名月份非法：{mm}")
    store = STORE_CODE_MAP[code.upper()]
    month_start = f"20{yy}-{month_num:02d}-01"
    return store, month_start


def add_month(month_start: str) -> str:
    dt = datetime.strptime(month_start, "%Y-%m-%d")
    if dt.month == 12:
        return f"{dt.year + 1:04d}-01-01"
    return f"{dt.year:04d}-{dt.month + 1:02d}-01"


def detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:4096]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("无法识别TXT编码，支持 UTF-8、UTF-16、GB18030")


def detect_delimiter(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, newline="") as handle:
        sample = handle.read(8192)
    first_line = sample.splitlines()[0] if sample else ""
    if "\t" in first_line:
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters="\t,;|").delimiter
    except csv.Error as exc:
        raise ValueError("无法识别TXT分隔符；Amazon All Orders报告通常应为制表符分隔") from exc


def env_required(name: str) -> str:
    value = clean(os.getenv(name))
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def connect_db():
    return pymysql.connect(
        host=env_required("LINGXING_DB_HOST"),
        port=int(os.getenv("LINGXING_DB_PORT", "3306")),
        user=env_required("LINGXING_DB_USER"),
        password=env_required("LINGXING_DB_PASSWORD"),
        database=os.getenv("LINGXING_DB_NAME", "lingxing"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=15,
        read_timeout=1200,
        write_timeout=1200,
    )


def load_table_columns(cur, schema: str, table: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            COLUMN_NAME AS column_name,
            DATA_TYPE AS data_type,
            COLUMN_TYPE AS column_type,
            IS_NULLABLE AS is_nullable,
            COLUMN_DEFAULT AS column_default,
            EXTRA AS extra
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    rows = list(cur.fetchall())
    if not rows:
        raise RuntimeError(f"目标表不存在：{schema}.{table}")
    return rows


def load_indexes(cur, schema: str, table: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT INDEX_NAME AS index_name, NON_UNIQUE AS non_unique,
               SEQ_IN_INDEX AS seq_in_index, COLUMN_NAME AS column_name
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
        """,
        (schema, table),
    )
    return list(cur.fetchall())


def parse_datetime_utc(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"无法解析日期时间：{text!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def convert_value(value: Any, data_type: str, column_name: str) -> Any:
    text = clean(value)
    if text == "":
        return None

    if column_name.endswith("_utc") or data_type in {"datetime", "timestamp"}:
        return parse_datetime_utc(text)
    if data_type == "date":
        dt = parse_datetime_utc(text)
        return dt.date() if dt else None
    if data_type in {"tinyint", "smallint", "mediumint", "int", "bigint"}:
        lowered = text.lower()
        if lowered in {"true", "yes", "y"}:
            return 1
        if lowered in {"false", "no", "n"}:
            return 0
        return int(Decimal(text))
    if data_type in {"decimal", "numeric", "float", "double", "real"}:
        try:
            return Decimal(text.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"字段 {column_name} 数值非法：{text!r}") from exc
    return text


def read_txt(path: Path, encoding: str, delimiter: str) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("TXT没有表头")
        headers = [normalize_name(h) for h in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for line_no, raw in enumerate(reader, start=2):
            normalized = {
                normalize_name(key): clean(value)
                for key, value in raw.items()
                if key is not None
            }
            if not any(normalized.values()):
                continue
            normalized["__line_no__"] = str(line_no)
            rows.append(normalized)
    if not rows:
        raise ValueError("TXT没有数据行")
    return headers, rows


def source_value(source: dict[str, str], destination_column: str) -> Any:
    aliases = SOURCE_ALIASES.get(destination_column, (destination_column,))
    for alias in aliases:
        if alias in source and clean(source.get(alias)) != "":
            return source.get(alias)
    return None


def build_insert_rows(
    source_rows: list[dict[str, str]],
    table_columns: list[dict[str, Any]],
    *,
    store_name: str,
    month_start: str,
    source_file: str,
) -> tuple[list[str], list[tuple[Any, ...]], dict[str, int]]:
    column_meta = {row["column_name"]: row for row in table_columns}
    generated_columns = {
        row["column_name"]
        for row in table_columns
        if "auto_increment" in clean(row.get("extra")).lower()
        or clean(row.get("extra")).lower().startswith("generated")
    }
    skip_columns = generated_columns | {"created_at", "updated_at", "synced_at"}

    insert_columns: list[str] = []
    for column_name in column_meta:
        if column_name in skip_columns:
            continue
        if column_name in {
            "store_name", "report_month", "source_file", "import_file",
            "file_name", "order_item_id", "source_hash"
        }:
            insert_columns.append(column_name)
            continue
        aliases = SOURCE_ALIASES.get(column_name, (column_name,))
        if any(any(alias in row for alias in aliases) for row in source_rows[:50]):
            insert_columns.append(column_name)

    for required in REQUIRED_BUSINESS_FIELDS:
        if required not in column_meta:
            raise RuntimeError(f"目标表缺少核心字段：{required}")
        if required not in insert_columns:
            insert_columns.append(required)

    missing_required: list[str] = []
    for name, meta in column_meta.items():
        if name in insert_columns or name in skip_columns:
            continue
        if (
            meta["is_nullable"] == "NO"
            and meta["column_default"] is None
            and "auto_increment" not in clean(meta.get("extra")).lower()
        ):
            missing_required.append(name)
    if missing_required:
        raise RuntimeError(
            "目标表存在TXT无法提供且无默认值的必填字段："
            + ", ".join(missing_required)
            + "。请先查看 SHOW COLUMNS FROM ods_amz_all_orders_report"
        )

    results: list[tuple[Any, ...]] = []
    stats = {"input_rows": 0, "accepted_rows": 0}
    for source in source_rows:
        stats["input_rows"] += 1
        line_no = source.get("__line_no__", "?")
        values: list[Any] = []
        try:
            for column_name in insert_columns:
                meta = column_meta[column_name]
                if column_name == "store_name":
                    raw_value = store_name
                elif column_name == "report_month":
                    raw_value = month_start
                elif column_name in {"source_file", "import_file", "file_name"}:
                    raw_value = source_file
                elif column_name == "order_item_id":
                    raw_value = source_value(source, column_name) or ""
                elif column_name == "source_hash":
                    raw_key = "|".join(
                        [
                            store_name,
                            clean(source_value(source, "amazon_order_id")),
                            clean(source_value(source, "sku")),
                            clean(source_value(source, "purchase_date_utc")),
                            clean(source_value(source, "quantity")),
                            clean(source_value(source, "item_price")),
                        ]
                    )
                    raw_value = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
                else:
                    raw_value = source_value(source, column_name)

                value = convert_value(raw_value, meta["data_type"], column_name)
                if column_name == "quantity" and (value is None or int(value) <= 0):
                    raise ValueError("quantity必须大于0")
                if column_name == "purchase_date_utc" and value is None:
                    raise ValueError("purchase_date不能为空")
                if column_name == "amazon_order_id" and not clean(value):
                    raise ValueError("amazon_order_id不能为空")
                if column_name == "sku" and not clean(value):
                    raise ValueError("sku不能为空")
                values.append(value)
        except Exception as exc:
            raise ValueError(f"TXT第{line_no}行转换失败：{exc}") from exc

        results.append(tuple(values))
        stats["accepted_rows"] += 1
    return insert_columns, results, stats


def validate_month(rows: list[tuple[Any, ...]], columns: list[str], month_start: str) -> tuple[datetime, datetime]:
    idx = columns.index("purchase_date_utc")
    dates = [row[idx] for row in rows if isinstance(row[idx], datetime)]
    if not dates:
        raise RuntimeError("没有可用的 purchase_date_utc")
    minimum, maximum = min(dates), max(dates)
    target_prefix = month_start[:7]
    month_hits = sum(1 for dt in dates if dt.strftime("%Y-%m") == target_prefix)
    if month_hits / len(dates) < 0.95:
        raise RuntimeError(
            f"文件日期与文件名月份不匹配：目标={target_prefix}，"
            f"UTC日期范围={minimum}~{maximum}，目标月占比={month_hits/len(dates):.2%}"
        )
    return minimum, maximum


def quote_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def chunks(rows: list[tuple[Any, ...]], size: int) -> Iterable[list[tuple[Any, ...]]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入本地 Amazon All Orders TXT 月累计文件")
    parser.add_argument("txt_file", help="TXT文件路径，如 /data/import/RKZ2607-.txt")
    parser.add_argument("--table", default="ods_amz_all_orders_report")
    parser.add_argument("--store", default="", help="覆盖文件名解析的店铺，如 RKZ-US")
    parser.add_argument("--month", default="", help="覆盖文件名解析的月份，如 2026-07-01")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--apply", action="store_true", help="正式写入数据库")
    parser.add_argument(
        "--replace-month",
        action="store_true",
        help="先删除目标店铺目标月份旧数据，再写入整份月累计TXT",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.txt_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"TXT文件不存在：{path}")

    parsed_store, parsed_month = parse_filename(path)
    store_name = clean(args.store) or parsed_store
    month_start = clean(args.month) or parsed_month
    datetime.strptime(month_start, "%Y-%m-%d")
    next_month = add_month(month_start)

    encoding = detect_encoding(path)
    delimiter = detect_delimiter(path, encoding)
    _, source_rows = read_txt(path, encoding, delimiter)

    db_name = os.getenv("LINGXING_DB_NAME", "lingxing")
    with connect_db() as conn:
        with conn.cursor() as cur:
            table_columns = load_table_columns(cur, db_name, args.table)
            indexes = load_indexes(cur, db_name, args.table)
            insert_columns, insert_rows, stats = build_insert_rows(
                source_rows,
                table_columns,
                store_name=store_name,
                month_start=month_start,
                source_file=path.name,
            )
            min_date, max_date = validate_month(insert_rows, insert_columns, month_start)

            cur.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM {quote_identifier(args.table)}
                WHERE store_name=%s
                  AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') >= %s
                  AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') < %s
                """,
                (store_name, month_start, next_month),
            )
            old_count = int(cur.fetchone()["cnt"])

            print("=" * 72)
            print("Amazon All Orders TXT 导入预检")
            print(f"文件：{path}")
            print(f"编码/分隔符：{encoding} / {'TAB' if delimiter == chr(9) else delimiter}")
            print(f"解析店铺：{store_name}")
            print(f"解析月份：{month_start[:7]}")
            print(f"TXT数据行：{stats['input_rows']:,}")
            print(f"待写入行：{stats['accepted_rows']:,}")
            print(f"文件UTC日期范围：{min_date} ~ {max_date}")
            print(f"目标月份数据库旧行数：{old_count:,}")
            print(f"写入字段数：{len(insert_columns)}")
            print("写入字段：" + ", ".join(insert_columns))
            unique_indexes: dict[str, list[str]] = {}
            for row in indexes:
                if int(row["non_unique"]) == 0:
                    unique_indexes.setdefault(row["index_name"], []).append(row["column_name"])
            print(f"唯一索引：{unique_indexes or '无业务唯一索引'}")
            print("=" * 72)

            if not args.apply:
                print("当前为预检模式，未修改数据库。")
                print("确认无误后加：--apply --replace-month")
                conn.rollback()
                return 0

            if not args.replace_month:
                raise RuntimeError(
                    "正式导入必须同时指定 --replace-month。"
                    "7月文件是月累计文件，禁止直接追加，以免重复。"
                )

            placeholders = ",".join(["%s"] * len(insert_columns))
            column_sql = ",".join(quote_identifier(c) for c in insert_columns)
            insert_sql = (
                f"INSERT INTO {quote_identifier(args.table)} "
                f"({column_sql}) VALUES ({placeholders})"
            )

            try:
                cur.execute(
                    f"""
                    DELETE FROM {quote_identifier(args.table)}
                    WHERE store_name=%s
                      AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') >= %s
                      AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') < %s
                    """,
                    (store_name, month_start, next_month),
                )
                deleted = cur.rowcount

                inserted = 0
                for batch in chunks(insert_rows, max(1, args.batch_size)):
                    cur.executemany(insert_sql, batch)
                    inserted += len(batch)
                    print(f"已写入：{inserted:,}/{len(insert_rows):,}")

                cur.execute(
                    f"""
                    SELECT
                        COUNT(*) AS row_count,
                        COUNT(DISTINCT amazon_order_id, sku) AS order_sku_count,
                        COALESCE(SUM(quantity),0) AS units,
                        ROUND(
                            SUM(item_price-COALESCE(item_promotion_discount,0))
                            / NULLIF(SUM(quantity),0),
                            4
                        ) AS weighted_deal_price
                    FROM {quote_identifier(args.table)}
                    WHERE store_name=%s
                      AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') >= %s
                      AND CONVERT_TZ(purchase_date_utc,'UTC','America/Los_Angeles') < %s
                    """,
                    (store_name, month_start, next_month),
                )
                audit = cur.fetchone()
                if int(audit["row_count"] or 0) != len(insert_rows):
                    raise RuntimeError(
                        f"写后行数不一致：文件={len(insert_rows)}, 数据库={audit['row_count']}"
                    )

                conn.commit()
                print("=" * 72)
                print("导入成功")
                print(f"删除旧行：{deleted:,}")
                print(f"写入新行：{inserted:,}")
                print(f"订单-SKU数：{int(audit['order_sku_count'] or 0):,}")
                print(f"销量：{int(audit['units'] or 0):,}")
                print(f"加权成交价：{audit['weighted_deal_price']}")
                print("=" * 72)
                return 0
            except Exception:
                conn.rollback()
                print("导入失败，事务已回滚，原有数据未被破坏。", file=sys.stderr)
                raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        raise
