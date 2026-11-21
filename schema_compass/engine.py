"""Matching engine: retrieve, score, attach evidence, (optionally) rerank, and explain."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .advisor import advise
from .catalog import Catalog, Column, Table
from .feedback import FeedbackStore
from .index import Embedder, SimilarityModel, readable_text
from .knowledge import Evidence, KnowledgeBase
from .text import role, role_compatibility, type_compatibility, type_family


@dataclass
class Weights:
    label: float = 0.55
    description: float = 0.45
    semantic: float = 0.45          # share given to dense similarity when an embedder is present
    table_coverage: float = 0.55    # share of table score from column coverage
    column_table_context: float = 0.30
    evidence_pull: float = 0.65     # how far positive evidence pulls a score toward 1
    evidence_push: float = 0.60     # how far negative evidence pushes a score toward 0
    min_parent_table: float = 0.45   # below this the parent table is not a plausible match
    llm_blend: float = 0.60         # share of final score from the LLM reranker when enabled


BANDS = (
    (80, "high", "Strong match: quick SME confirmation"),
    (60, "medium", "Likely match: standard review"),
    (40, "low", "Weak match: research before using"),
    (0, "none", "No confident match: likely new, derived or out of scope"),
)


def band(score: float, margin: float | None = None) -> tuple[str, str]:
    for floor, name, text in BANDS:
        if score >= floor:
            if name == "high" and margin is not None and margin < 8:
                return "medium", "Close alternatives: review the runner-up too"
            return name, text
    return BANDS[-1][1:]


def calibrate(raw: np.ndarray | float) -> np.ndarray | float:
    """Map raw TF-IDF-scale similarity to a 0..1 match strength.

    Lexical cosine between well-matched metadata rarely exceeds ~0.7, while unrelated
    fields sit below ~0.15. A smooth logistic maps that range onto 0..1."""
    return 1 / (1 + np.exp(-(np.asarray(raw) - 0.32) * 11))


class MatchEngine:
    def __init__(self, source: Catalog, target: Catalog, *, knowledge: KnowledgeBase | None = None,
                 feedback: FeedbackStore | None = None, embedder: Embedder | None = None,
                 reranker=None, weights: Weights | None = None):
        if not source.tables:
            raise ValueError("Source catalog is empty.")
        self.source, self.target = source, target
        self.knowledge = knowledge or KnowledgeBase()
        self.feedback = feedback
        self.reranker = reranker
        self.weights = weights or Weights()
        corpus = source.tables + target.tables + source.columns + target.columns
        self.model = SimilarityModel(corpus, embedder)
        self.src_tables = self.model.encode(source.tables)
        self.src_columns_list = source.columns
        self.src_columns = self.model.encode(self.src_columns_list)
        self._column_rows = {}
        for row, column in enumerate(self.src_columns_list):
            self._column_rows.setdefault(column.table.id, []).append(row)
        counts: dict[str, int] = {}
        for column in self.src_columns_list:
            counts[column.name.casefold()] = counts.get(column.name.casefold(), 0) + 1
        self._column_counts = counts
        self._table_cache: dict[str, list[dict]] = {}

    # ---------------------------------------------------------------- helpers
    def _blend(self, feats: dict) -> np.ndarray:
        w = self.weights
        lexical = (w.label * feats["label"] + w.description * feats["description"]) / (w.label + w.description)
        lexical = calibrate(lexical)
        if feats["semantic"] is None:
            return lexical
        # Dense cosine for MiniLM-class models: unrelated ~0.2, paraphrase ~0.7+
        dense = np.clip((feats["semantic"] - 0.2) / 0.55, 0, 1)
        return (1 - w.semantic) * lexical + w.semantic * dense

    def _evidence(self, target_table: Table, target_column: Column | None, source_table: Table,
                  source_column: Column | None) -> list[Evidence]:
        found = self.knowledge.s2t_evidence(target_table, target_column, source_table, source_column)
        unique = source_column is None or self._column_counts.get(source_column.name.casefold(), 0) <= 1
        found += self.knowledge.text_evidence(target_table, target_column, source_table, source_column,
                                              source_unique=unique)
        if self.feedback:
            found += self.feedback.evidence(target_table.id, target_column.name if target_column else None,
                                            source_table.id, source_column.name if source_column else None)
        return found

    def _apply_evidence(self, score: float, evidence: list[Evidence]) -> float:
        w = self.weights
        if any(e.kind == "feedback" and e.strength >= 1 for e in evidence):
            return max(score, 0.97)
        if any(e.kind == "feedback" and e.strength <= -1 for e in evidence):
            return min(score, 0.05)
        positive = max([e.strength for e in evidence if e.strength > 0], default=0.0)
        negative = min([e.strength for e in evidence if e.strength < 0], default=0.0)
        score = score + (1 - score) * w.evidence_pull * positive
        return score * (1 + w.evidence_push * negative)

    # ------------------------------------------------------------ table match
    def table_scores(self, target_table: Table, shortlist: int = 15) -> list[dict]:
        """Score every source table for a target table; deep-score the shortlist."""
        if target_table.id in self._table_cache:
            return self._table_cache[target_table.id]
        query = self.model.encode([target_table])
        feats = SimilarityModel.compare(query, self.src_tables)
        base = self._blend(feats)
        order = np.argsort(-base)
        target_cols = self.model.encode(target_table.columns) if target_table.columns else None
        results = []
        for position, index in enumerate(order):
            source_table = self.source.tables[index]
            coverage = None
            score = float(base[index])
            if position < shortlist:
                rows = self._column_rows.get(source_table.id, [])
                if target_cols is not None and rows:
                    pool = SimilarityModel.subset(self.src_columns, rows)
                    best = [float(self._blend(SimilarityModel.compare(
                        SimilarityModel.subset(target_cols, [i]), pool)).max())
                        for i in range(len(target_table.columns))]
                    coverage = float(np.mean(best))
                    w = self.weights.table_coverage
                    score = (1 - w) * score + w * coverage
                evidence = self._evidence(target_table, None, source_table, None)
                score = self._apply_evidence(score, evidence)
            else:
                evidence = []
                score *= 0.8
            results.append(dict(table=source_table, score=score, name_similarity=float(feats["label"][index]),
                                description_similarity=float(feats["description"][index]),
                                semantic_similarity=None if feats["semantic"] is None else float(feats["semantic"][index]),
                                coverage=coverage, evidence=evidence))
        results.sort(key=lambda r: (-r["score"], r["table"].name))
        self._table_cache[target_table.id] = results
        return results

    def _warn(self, target_table: Table, target_column: Column | None, candidates: list[dict]) -> list[dict]:
        warnings = self.knowledge.target_warnings(target_table, target_column)
        if warnings:
            factor = 1 + self.weights.evidence_push * min(w.strength for w in warnings)
            for cand in candidates:
                cand["score"] = round(cand["score"] * factor, 1)
                cand["evidence"] += [w.to_dict() for w in warnings]
        return [w.to_dict() for w in warnings]

    def match_table(self, target_table: Table, k: int = 5, rerank: bool = False) -> dict:
        scored = self.table_scores(target_table)
        candidates = []
        for rank, row in enumerate(scored[:k], 1):
            reasons = self._table_reasons(row)
            candidates.append(dict(
                rank=rank, source_table=row["table"].to_dict(), source_column=None,
                score=round(row["score"] * 100, 1),
                features=dict(name=round(row["name_similarity"], 3),
                              description=round(row["description_similarity"], 3),
                              semantic=None if row["semantic_similarity"] is None else round(row["semantic_similarity"], 3),
                              column_coverage=None if row["coverage"] is None else round(row["coverage"], 3)),
                evidence=[e.to_dict() for e in row["evidence"]], issues=[], reasons=reasons))
        warnings = self._warn(target_table, None, candidates)
        return self._finish(target_table, None, candidates, rerank, warnings,
                            considered=min(len(scored), 15))

    # ----------------------------------------------------------- column match
    def match_column(self, target_table: Table, target_column: Column, k: int = 5,
                     rerank: bool = False, table_pool: int = 5, global_pool: int = 25) -> dict:
        tables = self.table_scores(target_table)
        table_score = {r["table"].id: r["score"] for r in tables}
        table_rank = {r["table"].id: i for i, r in enumerate(tables, 1)}
        query = self.model.encode([target_column])
        feats = SimilarityModel.compare(query, self.src_columns)
        field = self._blend(feats)
        pool = set()
        for row in tables[:table_pool]:
            pool.update(self._column_rows.get(row["table"].id, []))
        pool.update(int(i) for i in np.argsort(-field)[:global_pool])
        for mapping in self.knowledge.mappings:  # anything an S2T sheet names for this field
            if mapping["target_column"].casefold() == target_column.name.casefold():
                for i, c in enumerate(self.src_columns_list):
                    if c.name.casefold() == mapping["source_column"].casefold():
                        pool.add(i)
        w = self.weights
        scored = []
        for index in pool:
            column = self.src_columns_list[index]
            parent = column.table
            context = table_score.get(parent.id, 0.0)
            score = (1 - w.column_table_context) * float(field[index]) + w.column_table_context * context
            issues = []
            compat, note = type_compatibility(column.data_type, target_column.data_type)
            if note:
                issues.append(note)
            if compat == 0:
                score = min(score * 0.5, 0.45)
            elif compat < 1:
                score *= 0.95
            fit = role_compatibility(role(column.name) or role(column.logical_name),
                                     role(target_column.name) or role(target_column.logical_name))
            score *= fit
            if fit <= 0.6:
                issues.append("Field roles differ (e.g. identifier vs name); check semantics.")
            if column.length and target_column.length and column.length > target_column.length \
                    and type_family(target_column.data_type) == "TEXT":
                issues.append(f"Source width {column.length} exceeds target width {target_column.length}; "
                              "profile values before loading.")
            if context < w.min_parent_table:
                score *= 0.6
                issues.append(f"Parent table {parent.short_name} is not a plausible match for "
                              f"{target_table.short_name}; this would need a join or derivation.")
            evidence = self._evidence(target_table, target_column, parent, column)
            score = self._apply_evidence(score, evidence)
            scored.append(dict(column=column, score=score, context=context, rank_of_table=table_rank.get(parent.id),
                               label=float(feats["label"][index]), description=float(feats["description"][index]),
                               semantic=None if feats["semantic"] is None else float(feats["semantic"][index]),
                               type=compat, issues=issues, evidence=evidence))
        scored.sort(key=lambda r: (-r["score"], r["column"].ref))
        candidates = []
        for rank, row in enumerate(scored[:k], 1):
            column = row["column"]
            candidates.append(dict(
                rank=rank, source_table=column.table.to_dict(), source_column=column.to_dict(),
                score=round(row["score"] * 100, 1),
                features=dict(name=round(row["label"], 3), description=round(row["description"], 3),
                              semantic=None if row["semantic"] is None else round(row["semantic"], 3),
                              table_context=round(row["context"], 3), type_compatibility=row["type"]),
                evidence=[e.to_dict() for e in row["evidence"]], issues=row["issues"],
                reasons=self._column_reasons(row)))
        warnings = self._warn(target_table, target_column, candidates)
        return self._finish(target_table, target_column, candidates, rerank, warnings, considered=len(pool))

    def map_table(self, target_table: Table, k: int = 3, rerank: bool = False) -> dict:
        """Map every column of a target table (a draft S2T sheet)."""
        table = self.match_table(target_table, k=k)
        columns = [self.match_column(target_table, c, k=k, rerank=rerank) for c in target_table.columns]
        return dict(target=target_table.to_dict(), table=table, columns=columns)

    # ------------------------------------------------------------- reporting
    def _finish(self, target_table: Table, target_column: Column | None, candidates: list[dict],
                rerank: bool, warnings: list[dict] | None = None, considered: int = 0) -> dict:
        target_item = target_column or target_table
        query = readable_text(target_item) + (f" in {readable_text(target_table)}" if target_column else "")
        context = [dict(chunk.to_dict(), score=round(score, 3))
                   for chunk, score in self.knowledge.search(query, k=4)]
        llm = None
        if rerank and self.reranker is not None and candidates:
            llm = self.reranker.rerank(target_table, target_column, candidates, context)
            if llm is not None:
                by_ref = {c["llm_ref"]: c for c in llm.get("candidates", [])}
                for i, cand in enumerate(candidates, 1):
                    verdict = by_ref.get(f"C{i}")
                    if verdict:
                        cand["llm"] = verdict
                        cand["heuristic_score"] = cand["score"]
                        cand["score"] = round((1 - self.weights.llm_blend) * cand["score"]
                                              + self.weights.llm_blend * verdict["confidence"], 1)
                candidates.sort(key=lambda c: -c["score"])
                for i, cand in enumerate(candidates, 1):
                    cand["rank"] = i
        top = candidates[0]["score"] if candidates else 0.0
        margin = top - candidates[1]["score"] if len(candidates) > 1 else top
        level, text = band(top, margin)
        for cand in candidates:
            cand["band"] = band(cand["score"])[0]
        if llm and llm.get("no_match") and level in {"high", "medium"}:
            level, text = "low", "LLM reviewer doubts every candidate: research before using"
        trace = self._trace(target_table, target_column, considered, candidates, context, warnings or [],
                            rerank, llm)
        result = dict(
            level="column" if target_column else "table",
            target=dict(table=target_table.to_dict(), column=target_column.to_dict() if target_column else None),
            recommendation=dict(band=level, text=text, confidence=round(top, 1), margin=round(margin, 1),
                                summary=(llm or {}).get("summary"),
                                transformation=(llm or {}).get("transformation")),
            candidates=candidates, context=context, warnings=warnings or [], reranked=llm is not None,
            trace=trace)
        result["advice"] = advise(result)
        return result

    def _trace(self, target_table: Table, target_column: Column | None, considered: int,
               candidates: list[dict], context: list[dict], warnings: list[dict], rerank: bool,
               llm: dict | None) -> list[dict]:
        """A plain account of what the engine did, shown to users as "How we found this"."""
        item = target_column or target_table
        used = [label for label, value in (("business name", item.logical_name),
                                           ("description", item.description),
                                           ("data type", getattr(item, "data_type", None)),
                                           ("length", getattr(item, "length", None))) if value]
        kinds: dict[str, int] = {}
        for chunk in self.knowledge.chunks:
            kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
        evidence = sum(len(c["evidence"]) for c in candidates)
        steps = [
            dict(step="Read the target definition",
                 detail=f"Used the physical name{', ' + ', '.join(used) if used else ''} of {item.name}."
                        + ("" if used else " No description was available, so the match relies on the name.")),
            dict(step="Searched the source metadata",
                 detail=f"Compared against {len(self.source)} tables and {len(self.source.columns)} columns using "
                        f"keyword and meaning-based similarity"
                        + (" plus embeddings." if self.model.embedder else ".")),
            dict(step="Shortlisted candidates",
                 detail=f"Scored {considered} {'columns' if target_column else 'tables'} in detail, checking "
                        + ("data type, length, field role and parent table." if target_column
                           else "how many target columns each table can supply.")),
            dict(step="Checked team knowledge",
                 detail=(f"Searched {kinds.get('s2t', 0)} S2T mapping rows, {kinds.get('slack', 0)} Slack threads and "
                         f"{kinds.get('doc', 0)} document sections; found {evidence} piece(s) of evidence"
                         + (f" and {len(warnings)} warning(s)." if warnings else ".")) if self.knowledge.chunks
                 else "No S2T sheets, Slack exports or documents have been added to this project yet."),
        ]
        if llm is not None:
            steps.append(dict(step="AI review", detail=f"{llm.get('model', 'Claude')} reviewed the top "
                                                       f"{len(llm.get('candidates', []))} candidates and the evidence."))
        elif rerank:
            steps.append(dict(step="AI review", detail="Requested but unavailable; showing the standard ranking."))
        return steps

    @staticmethod
    def _pct(value: float) -> str:
        return f"{value * 100:.0f}%"

    def _table_reasons(self, row: dict) -> list[str]:
        reasons = [f"Name similarity {self._pct(row['name_similarity'])}, "
                   f"description similarity {self._pct(row['description_similarity'])}."]
        if row["semantic_similarity"] is not None:
            reasons.append(f"Semantic similarity {self._pct(row['semantic_similarity'])}.")
        if row["coverage"] is not None:
            reasons.append(f"Average best column match {self._pct(row['coverage'])} across target columns.")
        reasons += [e.summary for e in row["evidence"]]
        return reasons

    def _column_reasons(self, row: dict) -> list[str]:
        column = row["column"]
        reasons = [f"Name similarity {self._pct(row['label'])}, description similarity "
                   f"{self._pct(row['description'])}."]
        if row["semantic"] is not None:
            reasons.append(f"Semantic similarity {self._pct(row['semantic'])}.")
        if row["rank_of_table"]:
            reasons.append(f"Parent table {column.table.short_name} is table match #{row['rank_of_table']} "
                           f"({self._pct(row['context'])}).")
        if row["type"] == 1:
            reasons.append(f"Datatypes align ({column.data_type}).")
        reasons += [e.summary for e in row["evidence"]]
        return reasons
