"""裁决目录导出：指标目录与别名表，按所选裁决版本读取。"""

from __future__ import annotations

import csv
import io

CATALOG_HEADER = [
    "ruling_seq", "metric", "canonical_name", "version", "source",
    "signature", "depends_on", "group_status", "adopted", "reason",
]

ALIAS_HEADER = ["alias", "target", "canonical", "status", "detail"]


def _buffer() -> tuple[io.StringIO, "csv.writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def catalog_csv(rows: list[dict]) -> str:
    buffer, writer = _buffer()
    writer.writerow(CATALOG_HEADER)
    for row in rows:
        writer.writerow([
            row.get("ruling_seq") or "",
            row.get("metric") or "",
            row.get("canonical_name") or "",
            row.get("version") or "",
            row.get("source") or "",
            row.get("signature") or "",
            row.get("depends_on") or "",
            row.get("group_status") or "",
            row.get("adopted") or "",
            row.get("reason") or "",
        ])
    return buffer.getvalue()


def aliases_csv(rows: list[dict]) -> str:
    buffer, writer = _buffer()
    writer.writerow(ALIAS_HEADER)
    for row in rows:
        writer.writerow([
            row.get("alias") or "",
            row.get("target") or "",
            row.get("canonical") or "",
            row.get("status") or "",
            row.get("detail") or "",
        ])
    return buffer.getvalue()
