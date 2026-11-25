"""Retrieval-augmented evidence from S2T mapping docs, Slack threads and design notes.

The knowledge base does two jobs:

1. **Pair evidence** - given a target field and a candidate source field, find documents that
   explicitly map them (S2T sheets), or discuss both (Slack/notes), and judge whether the
   discussion endorses or warns against the candidate.
2. **Context retrieval** - return the passages most related to a target so analysts (and the
   optional LLM reranker) can read the tribal knowledge behind a recommendation.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .catalog import Column, Table
from .text import normalize, short_name

S2T_ALIASES = {
    "target_table": ("target_table", "tgt_table", "target table", "target_table_name"),
    "target_column": ("target_column", "tgt_column", "target column", "target_field"),
    "source_table": ("source_table", "src_table", "source table", "source_table_name"),
    "source_column": ("source_column", "src_column", "source column", "source_field"),
    "transformation": ("transformation", "transform", "rule", "logic", "derivation"),
    "notes": ("notes", "comment", "comments", "remarks"),
}

# Statements about the source itself: they hold whatever the target is.
ABSOLUTE_AFTER = r"[^.\n]{0,60}?\b(deprecated|retired|stale|frozen|obsolete|no longer (?:used|maintained|updated)|should not be used)"
ABSOLUTE_BEFORE = r"\b(do not use|don't use|never use|avoid)\s+(?:the\s+)?(?:[\w.]*\.)?"
# Statements relative to a target ("use X, not Y"): they count only when the same sentence names the target.
RELATIVE_BEFORE = r"\b(not|instead of|rather than|ignore)\s+(?:the\s+)?(?:[\w.]*\.)?"
CAUTION = re.compile(r"(heads up|careful|caution|cleansing|cannot be cast|can't cast|plain cast|qualifier|"
                     r"expect blanks|time ?zone|pacific|eastern|local time|percentage|fraction|/ ?100|"
                     r"same scale|different scale|format)", re.I)
NO_SOURCE = re.compile(r"(nothing to backfill|no (?:direct )?source|never had|starts? fresh|net[- ]new|no equivalent|"
                       r"not available in (?:the )?source|does not exist in (?:the )?source|no history|"
                       r"never stored|does not store|not stored|never captured|never fed|never synced)", re.I)
SENTENCES = re.compile(r"(?<=[.!?\n;])\s+")
POSITIVE = re.compile(r"(maps? to|map\b|mapped|->|→|=|same as|equivalent|corresponds|use\b|is actually|comes from|source(d)? from|populate)", re.I)


@dataclass
class Chunk:
    id: str
    kind: str  # s2t | slack | doc
    source: str
    title: str
    text: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(id=self.id, kind=self.kind, source=self.source, title=self.title,
                    text=self.text, meta=self.meta)


@dataclass
class Evidence:
    kind: str  # s2t | slack | doc | feedback
    strength: float  # -1 (warns against) .. +1 (explicit mapping)
    summary: str
    source: str
    snippet: str = ""

    def to_dict(self) -> dict:
        return dict(kind=self.kind, strength=round(self.strength, 2), summary=self.summary,
                    source=self.source, snippet=self.snippet)


def _pattern(identifier: str) -> re.Pattern:
    return re.compile(rf"(?<![\w]){re.escape(identifier)}(?![\w])", re.I)


def _mentions(text: str, identifier: str | None) -> bool:
    return bool(identifier) and bool(_pattern(identifier).search(text))


class KnowledgeBase:
    def __init__(self):
        self.chunks: list[Chunk] = []
        self.mappings: list[dict] = []  # structured S2T rows
        self._vectorizer = None
        self._matrix = None

    # ---------------------------------------------------------------- ingest
    def add_path(self, path: str | Path) -> int:
        """Ingest a file or every supported file in a directory. Returns chunks added."""
        path = Path(path)
        if path.is_dir():
            return sum(self.add_path(p) for p in sorted(path.rglob("*")) if p.is_file())
        before = len(self.chunks)
        suffix = path.suffix.lower()
        if suffix == ".csv":
            self.add_s2t_csv(path)
        elif suffix == ".json":
            self.add_slack_export(path)
        elif suffix in {".md", ".txt"}:
            self.add_document(path)
        elif suffix == ".xlsx":
            self.add_s2t_xlsx(path)
        self._vectorizer = None
        return len(self.chunks) - before

    def add_s2t_rows(self, rows, source: str):
        for number, raw in enumerate(rows, 1):
            lowered = {str(k).strip().lower(): (str(v).strip() if v is not None else "")
                       for k, v in raw.items() if k is not None}
            row = {key: next((lowered[a] for a in aliases if lowered.get(a)), "")
                   for key, aliases in S2T_ALIASES.items()}
            if not row["target_table"] or not row["source_table"]:
                continue
            row["source"] = f"{source} row {number}"
            self.mappings.append(row)
            text = (f"{row['target_table']}.{row['target_column']} <- "
                    f"{row['source_table']}.{row['source_column']}. "
                    f"{row['transformation']} {row['notes']}").strip()
            self.chunks.append(Chunk(id=f"{source}#{number}", kind="s2t", source=source,
                                     title=f"S2T: {row['target_table']}.{row['target_column']}",
                                     text=text, meta=row))
        self._vectorizer = None

    def add_s2t_csv(self, path: Path):
        with open(path, newline="", encoding="utf-8") as handle:
            self.add_s2t_rows(csv.DictReader(handle), path.name)

    def add_s2t_xlsx(self, path: Path):
        from openpyxl import load_workbook  # optional dependency

        sheet = load_workbook(path, read_only=True).active
        rows = sheet.iter_rows(values_only=True)
        header = [str(h or "") for h in next(rows)]
        self.add_s2t_rows((dict(zip(header, r)) for r in rows), path.name)

    def add_slack_export(self, path: Path):
        """Accept a Slack channel export (list of messages) and group messages by thread."""
        data = json.loads(path.read_text(encoding="utf-8"))
        messages = data.get("messages", data) if isinstance(data, dict) else data
        channel = data.get("channel", path.stem) if isinstance(data, dict) else path.stem
        threads: dict[str, list[dict]] = {}
        for message in messages:
            if not message.get("text"):
                continue
            key = str(message.get("thread_ts") or message.get("ts"))
            threads.setdefault(key, []).append(message)
        for key, items in threads.items():
            items.sort(key=lambda m: float(m.get("ts", 0)))
            lines = [f"{m.get('user_name') or m.get('user', 'unknown')}: {m['text']}" for m in items]
            when = datetime.fromtimestamp(float(items[0].get("ts", 0)), tz=timezone.utc).date()
            self.chunks.append(Chunk(id=f"{path.name}#{key}", kind="slack", source=f"#{channel}",
                                     title=f"Slack #{channel} thread ({when})",
                                     text="\n".join(lines),
                                     meta=dict(date=str(when), messages=len(items))))
        self._vectorizer = None

    def add_document(self, path: Path, max_chars: int = 1200):
        text = path.read_text(encoding="utf-8")
        sections = re.split(r"\n(?=#{1,4} )", text)
        for number, section in enumerate(sections, 1):
            heading = section.strip().splitlines()[0].lstrip("# ").strip() if section.strip() else ""
            paragraphs, buffer = re.split(r"\n\s*\n", section), ""
            part = 0
            for paragraph in paragraphs + [None]:
                if paragraph is None or (buffer and len(buffer) + len(paragraph) > max_chars):
                    if buffer.strip():
                        part += 1
                        self.chunks.append(Chunk(id=f"{path.name}#{number}.{part}", kind="doc",
                                                 source=path.name,
                                                 title=f"{path.name} › {heading}" if heading else path.name,
                                                 text=buffer.strip()))
                    buffer = ""
                if paragraph is not None:
                    buffer += paragraph + "\n\n"
        self._vectorizer = None

    # ------------------------------------------------------------- retrieval
    def _ensure_index(self):
        if self._vectorizer is None and self.chunks:
            self._vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True,
                                               token_pattern=r"(?u)\b\w+\b")
            self._matrix = self._vectorizer.fit_transform(
                [normalize(c.title + " " + c.text) for c in self.chunks])

    def search(self, query: str, k: int = 5, min_score: float = 0.05) -> list[tuple[Chunk, float]]:
        self._ensure_index()
        if self._vectorizer is None:
            return []
        scores = (self._vectorizer.transform([normalize(query)]) @ self._matrix.T).toarray()[0]
        order = np.argsort(-scores)[:k]
        return [(self.chunks[i], float(scores[i])) for i in order if scores[i] >= min_score]

    # -------------------------------------------------------------- evidence
    @staticmethod
    def _same_table(reference: str, table: Table) -> bool:
        ref = reference.strip().casefold()
        return ref in {table.name.casefold(), table.short_name.casefold(),
                       (table.logical_name or "").casefold()} or \
            short_name(ref) == table.short_name.casefold()

    def s2t_evidence(self, target_table: Table, target_column: Column | None,
                     source_table: Table, source_column: Column | None) -> list[Evidence]:
        found = []
        for row in self.mappings:
            if not (self._same_table(row["target_table"], target_table)
                    and self._same_table(row["source_table"], source_table)):
                continue
            if target_column is None:
                found.append(Evidence("s2t", 0.8, "Prior S2T sheet maps these tables.", row["source"],
                                      f"{row['target_table']} <- {row['source_table']}"))
                break
            if source_column is None or row["target_column"].casefold() != target_column.name.casefold():
                continue
            if row["source_column"].casefold() == source_column.name.casefold():
                rule = f" Rule: {row['transformation']}" if row["transformation"] else ""
                found.append(Evidence("s2t", 1.0, "Prior S2T sheet maps this field." + rule,
                                      row["source"], f"{row['target_column']} <- {row['source_column']}"
                                      + (f" ({row['notes']})" if row["notes"] else "")))
            else:
                found.append(Evidence("s2t", -0.5, f"Prior S2T sheet maps this field from "
                                      f"{row['source_column']} instead.", row["source"]))
        return found

    def target_warnings(self, target_table: Table, target_column: Column | None) -> list[Evidence]:
        """Passages saying the target has no counterpart in the source (net-new or derived data).

        A passage about the target table counts for all of its columns, unless it names specific
        columns, in which case it counts only for those."""
        table_terms = [t for t in (target_table.short_name, target_table.logical_name) if t]
        found = []
        for chunk in self.chunks:
            if chunk.kind == "s2t" or not NO_SOURCE.search(chunk.text):
                continue
            text = chunk.text
            named = [c for c in target_table.columns if _mentions(text, c.name)]
            if target_column is not None and target_column in named:
                applies = True
            else:
                applies = not named and any(_mentions(text, t) for t in table_terms)
            if applies:
                sentence = next((s.strip() for s in SENTENCES.split(text) if NO_SOURCE.search(s)), text[:200])
                found.append(Evidence(chunk.kind, -0.8, f"{chunk.title} says this target has no source.",
                                      chunk.source, sentence[:300]))
        return found

    def text_evidence(self, target_table: Table, target_column: Column | None,
                      source_table: Table, source_column: Column | None,
                      source_unique: bool = True) -> list[Evidence]:
        """Judge each passage sentence by sentence.

        Endorsements and "use X, not Y" statements count only in sentences that name the target;
        deprecation statements count wherever they appear; cautions become checks, not votes."""
        found = []
        if target_column is not None:
            target_terms = [t for t in (target_column.name, target_column.logical_name) if t]
        else:
            target_terms = [t for t in (target_table.short_name, target_table.logical_name) if t]
        source_ident = source_column.name if source_column else source_table.short_name
        ident = re.escape(source_ident)
        for chunk in self.chunks:
            if chunk.kind == "s2t":
                continue
            text = chunk.text
            if not _mentions(text, source_ident):
                continue
            if source_column and not source_unique and not _mentions(text, source_table.short_name):
                continue  # generic column name, needs its table in the same passage
            chunk_names_target = any(_mentions(text, t) for t in target_terms)
            verdict, best, caution = None, None, None
            for sentence in (s.strip() for s in SENTENCES.split(text)):
                if not _mentions(sentence, source_ident):
                    continue
                names_target = any(_mentions(sentence, t) for t in target_terms)
                absolute = re.search(rf"(?<![\w]){ident}(?![\w])" + ABSOLUTE_AFTER, sentence, re.I) or \
                    re.search(ABSOLUTE_BEFORE + ident + r"(?![\w])", sentence, re.I)
                relative = re.search(RELATIVE_BEFORE + ident + r"(?![\w])", sentence, re.I)
                if absolute or (relative and names_target):
                    candidate = (-0.7, "warns against")
                elif names_target and POSITIVE.search(sentence):
                    candidate = (0.7, "endorses")
                elif names_target:
                    candidate = (0.45, "mentions")
                else:
                    candidate = None
                if candidate and (verdict is None or abs(candidate[0]) > abs(verdict[0])
                                  or (candidate[0] < 0 < verdict[0])):
                    verdict, best = candidate, sentence
                if CAUTION.search(sentence) and (names_target or chunk_names_target) and caution is None:
                    caution = sentence
            if verdict is None and chunk_names_target:
                verdict, best = (0.25, "mentions"), next(
                    (s for s in SENTENCES.split(text) if _mentions(s, source_ident)), text[:200])
            label = chunk.title
            if verdict is not None:
                strength, verb = verdict
                found.append(Evidence(chunk.kind, strength, f"{label} {verb} {source_ident}"
                                      + ("" if strength < 0 else " for this target") + ".",
                                      chunk.source, best.strip()[:300]))
            if caution is not None and (verdict is None or verdict[0] > 0):
                found.append(Evidence(chunk.kind, 0.0, f"{label} notes a caveat about {source_ident}.",
                                      chunk.source, caution.strip()[:300]))
        return found
