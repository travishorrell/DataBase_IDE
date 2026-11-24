"""Import and export: CSV, JSON and SQL INSERT scripts."""
from __future__ import annotations

import base64
import csv
import datetime as dt
import decimal
import json
from typing import Iterable

from dbide.drivers.base import Driver

FORMATS = {"CSV": "csv", "JSON": "json", "SQL INSERT": "sql"}


def to_text(value) -> str:
    """How a cell value is shown in grids and written to CSV."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + b.hex()
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    return str(value)


def _json_default(value):
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, dt.timedelta):
        return str(value)
    return str(value)


def sql_literal(value, driver: Driver) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"X'{bytes(value).hex()}'"
    return driver.literal(to_text(value))


def export_rows(path: str, fmt: str, columns: list[str], rows: Iterable, driver: Driver, table: str = "data") -> int:
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        if fmt == "csv":
            w = csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow(["" if v is None else to_text(v) for v in r])
                count += 1
        elif fmt == "json":
            f.write("[\n")
            for r in rows:
                if count:
                    f.write(",\n")
                f.write("  " + json.dumps(dict(zip(columns, r)), default=_json_default, ensure_ascii=False))
                count += 1
            f.write("\n]\n")
        elif fmt == "sql":
            cols = ", ".join(driver.quote(c) for c in columns)
            t = driver.quote(table)
            for r in rows:
                vals = ", ".join(sql_literal(v, driver) for v in r)
                f.write(f"INSERT INTO {t} ({cols}) VALUES ({vals});\n")
                count += 1
        else:
            raise ValueError(f"Unknown export format: {fmt}")
    return count


def read_csv(path: str, empty_as_null: bool = True) -> tuple[list[str], list[list]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(f, dialect)
        header = next(reader, [])
        rows = []
        for r in reader:
            if not r:
                continue
            r = (r + [""] * len(header))[: len(header)]
            rows.append([None if (empty_as_null and v == "") else v for v in r])
    return header, rows


def read_json(path: str) -> tuple[list[str], list[list]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ValueError("JSON import expects a list of objects.")
    cols: list[str] = []
    for obj in data:
        for k in obj:
            if k not in cols:
                cols.append(k)
    rows = [[obj.get(c) for c in cols] for obj in data]
    rows = [[json.dumps(v) if isinstance(v, (dict, list)) else v for v in r] for r in rows]
    return cols, rows
