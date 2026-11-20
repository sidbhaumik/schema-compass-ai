"""Measure match quality against a hand-labeled gold mapping file.

Gold CSV columns: target_table, target_column, source_table, source_column.
A blank target_column marks a table-level label; a blank source means "no valid source".
"""
from __future__ import annotations

import csv
from pathlib import Path

from .engine import MatchEngine

ABSTAIN_BELOW = 40.0  # a top score under this counts as "no confident match"


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def evaluate(engine: MatchEngine, gold_path: str | Path, k: int = 3) -> dict:
    with open(gold_path, newline="", encoding="utf-8") as handle:
        gold = list(csv.DictReader(handle))
    stats = {level: dict(total=0, top1=0, recall_at_k=0, matchable=0, abstain_correct=0,
                         unmatchable=0, false_abstain=0) for level in ("table", "column")}
    misses = []
    for row in gold:
        table = engine.target.find_table(row["target_table"])
        if row["target_column"]:
            level = "column"
            column = table.column(row["target_column"])
            result = engine.match_column(table, column, k=k)
            refs = [(c["source_table"]["name"], c["source_column"]["name"]) for c in result["candidates"]]
            expected = (row["source_table"], row["source_column"])
            hit = lambda ref: _same(ref[0], expected[0]) and _same(ref[1], expected[1])
        else:
            level = "table"
            result = engine.match_table(table, k=k)
            refs = [(c["source_table"]["name"], None) for c in result["candidates"]]
            expected = (row["source_table"], None)
            hit = lambda ref: _same(ref[0], expected[0])
        s = stats[level]
        s["total"] += 1
        top = result["recommendation"]["confidence"]
        abstained = top < ABSTAIN_BELOW
        if expected[0]:
            s["matchable"] += 1
            correct = bool(refs) and hit(refs[0]) and not abstained
            s["top1"] += correct
            s["recall_at_k"] += any(hit(r) for r in refs)
            s["false_abstain"] += abstained and bool(refs) and hit(refs[0])
            if not correct:
                misses.append(dict(level=level, target=f"{row['target_table']}.{row['target_column']}".rstrip("."),
                                   expected=".".join(filter(None, expected)),
                                   got=".".join(filter(None, refs[0])) if refs else None, score=top))
        else:
            s["unmatchable"] += 1
            s["abstain_correct"] += abstained
            if not abstained:
                misses.append(dict(level=level, target=f"{row['target_table']}.{row['target_column']}".rstrip("."),
                                   expected="(no match)", got=".".join(filter(None, refs[0])), score=top))
    summary = {}
    for level, s in stats.items():
        if not s["total"]:
            continue
        summary[level] = dict(
            labels=s["total"],
            top1_accuracy=round(s["top1"] / s["matchable"], 3) if s["matchable"] else None,
            recall_at_k=round(s["recall_at_k"] / s["matchable"], 3) if s["matchable"] else None,
            abstention_accuracy=round(s["abstain_correct"] / s["unmatchable"], 3) if s["unmatchable"] else None,
            false_abstentions=s["false_abstain"], k=k)
    return dict(summary=summary, misses=misses)
