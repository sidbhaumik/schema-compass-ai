"""Flatten match results into spreadsheet rows (S2T drafts and bulk results)."""
from __future__ import annotations

import csv
import io

COLUMNS = ["target_table", "target_column", "target_datatype", "decision", "confidence",
           "suggested_source_table", "suggested_source_column", "source_datatype", "source_business_name",
           "recommendation", "why", "checks_before_use", "evidence", "alternative_1", "alternative_2",
           "reviewer_status"]


def result_row(result: dict, status: str = "") -> dict:
    target = result["target"]
    advice = result["advice"]
    top = result["candidates"][0] if result["candidates"] and advice["decision"] != "no_source" else None
    alts = [f"{a['name']} ({a['score']:.0f})" for a in advice["alternatives"]]
    column = target["column"] or {}
    source_item = (top or {}).get("source_column") or (top or {}).get("source_table") or {}
    return dict(
        target_table=target["table"]["name"], target_column=column.get("name", ""),
        target_datatype=column.get("data_type") or "", decision=advice["label"],
        confidence=result["recommendation"]["confidence"],
        suggested_source_table=top["source_table"]["name"] if top else "",
        suggested_source_column=(top.get("source_column") or {}).get("name", "") if top else "",
        source_datatype=(top.get("source_column") or {}).get("data_type", "") if top else "",
        source_business_name=source_item.get("logical_name") or "",
        recommendation=advice["headline"], why=" | ".join(advice["why"]),
        checks_before_use=" | ".join(advice["checks"]),
        evidence=" | ".join(f"{e['summary']} ({e['source']})" for e in (top or {}).get("evidence", [])),
        alternative_1=alts[0] if alts else "", alternative_2=alts[1] if len(alts) > 1 else "",
        reviewer_status=status)


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def bulk_requests(rows: list[dict]) -> list[tuple[str, str]]:
    """Read target table/column pairs from an uploaded list with flexible headers."""
    import re

    key = lambda h: re.sub(r"[^a-z0-9]+", "_", str(h or "").lower()).strip("_")
    if not rows:
        raise ValueError("The file has no rows.")
    headers = {key(h): h for h in rows[0]}
    table_h = next((headers[k] for k in ("target_table", "table", "table_name", "target_table_name",
                                         "table_physical_name") if k in headers), None)
    column_h = next((headers[k] for k in ("target_column", "column", "column_name", "target_column_name",
                                          "column_physical_name", "field") if k in headers), None)
    if table_h is None:
        raise ValueError("Add a 'target_table' column (and optionally 'target_column').")
    pairs = []
    for row in rows:
        table = str(row.get(table_h) or "").strip()
        if table:
            pairs.append((table, str(row.get(column_h) or "").strip() if column_h else ""))
    if not pairs:
        raise ValueError("No target tables found in the file.")
    return pairs
