"""FastAPI service and analyst web UI."""
from __future__ import annotations

import csv
import io
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .app import Settings, build_engine
from .engine import MatchEngine

STATIC = Path(__file__).parent / "static"
UPLOAD_TYPES = {".csv", ".json", ".md", ".txt", ".xlsx"}


class MatchRequest(BaseModel):
    table: str
    column: str | None = None
    platform: str | None = None
    k: int = Field(default=5, ge=1, le=20)
    rerank: bool = False


class FeedbackRequest(BaseModel):
    target_table: str
    target_column: str | None = None
    source_table: str
    source_column: str | None = None
    decision: str
    reviewer: str
    note: str = ""
    confidence: float | None = None


def create_app(settings: Settings, engine: MatchEngine | None = None) -> FastAPI:
    app = FastAPI(title="Schema Compass", version="2.0")
    state = dict(engine=engine or build_engine(settings))
    lock = threading.Lock()

    def eng() -> MatchEngine:
        return state["engine"]

    def resolve(table: str, column: str | None, platform: str | None):
        try:
            target = eng().target.find_table(table, platform or None)
        except ValueError as error:
            raise HTTPException(404, str(error))
        field = None
        if column and column.strip():
            field = target.column(column)
            if field is None:
                raise HTTPException(404, f"Column '{column}' is not in {target.name}.")
        return target, field

    def need_reranker(rerank: bool):
        if rerank and eng().reranker is None:
            raise HTTPException(400, "AI review is not configured (no Anthropic credentials).")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        e = eng()
        kinds: dict[str, int] = {}
        for chunk in e.knowledge.chunks:
            kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
        return dict(source=dict(tables=len(e.source), columns=len(e.source.columns)),
                    target=dict(tables=len(e.target), columns=len(e.target.columns)),
                    knowledge=kinds, s2t_rows=len(e.knowledge.mappings),
                    reviews=len(e.feedback.latest()) if e.feedback else 0,
                    embeddings=e.model.embedder.model_name if e.model.embedder else None,
                    llm=getattr(e.reranker, "model", None))

    @app.get("/api/tables")
    def tables(side: str = "target", q: str = "", limit: int = 50):
        catalog = eng().target if side == "target" else eng().source
        term = q.strip().casefold()
        rows = [t.to_dict() for t in catalog.tables
                if not term or term in t.name.casefold() or term in (t.logical_name or "").casefold()]
        return rows[:limit]

    @app.get("/api/table")
    def table(name: str, side: str = "target", platform: str | None = None):
        catalog = eng().target if side == "target" else eng().source
        try:
            return catalog.find_table(name, platform).to_dict(columns=True)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.post("/api/match")
    def match(request: MatchRequest):
        need_reranker(request.rerank)
        target, field = resolve(request.table, request.column, request.platform)
        if field is None:
            return eng().match_table(target, k=request.k, rerank=request.rerank)
        return eng().match_column(target, field, k=request.k, rerank=request.rerank)

    @app.post("/api/map-table")
    def map_table(request: MatchRequest):
        need_reranker(request.rerank)
        target, _ = resolve(request.table, None, request.platform)
        return eng().map_table(target, k=min(request.k, 3), rerank=request.rerank)

    @app.get("/api/map-table.csv")
    def map_table_csv(table: str, platform: str | None = None):
        target, _ = resolve(table, None, platform)
        result = eng().map_table(target, k=3)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["target_table", "target_column", "target_type", "suggested_source_table",
                         "suggested_source_column", "source_type", "confidence", "band", "issues",
                         "evidence", "alternative_1", "alternative_2"])
        for col in result["columns"]:
            cands = col["candidates"]
            top = cands[0] if cands else None
            alts = [f"{c['source_table']['name']}.{c['source_column']['name']} ({c['score']})" for c in cands[1:3]]
            writer.writerow([
                target.name, col["target"]["column"]["name"], col["target"]["column"]["data_type"],
                top["source_table"]["name"] if top else "", top["source_column"]["name"] if top else "",
                top["source_column"]["data_type"] if top else "", col["recommendation"]["confidence"],
                col["recommendation"]["band"], "; ".join(top["issues"]) if top else "",
                "; ".join(e["summary"] for e in top["evidence"]) if top else "", *(alts + ["", ""])[:2]])
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", target.name)
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="s2t_{name}.csv"'})

    @app.post("/api/feedback")
    def feedback(request: FeedbackRequest):
        e = eng()
        if e.feedback is None:
            raise HTTPException(400, "Feedback store is disabled.")
        try:
            review_id = e.feedback.record(**request.model_dump())
        except ValueError as error:
            raise HTTPException(400, str(error))
        e._table_cache.clear()
        return dict(id=review_id)

    @app.get("/api/feedback")
    def feedback_history(limit: int = 100):
        return eng().feedback.history(limit) if eng().feedback else []

    @app.get("/api/knowledge")
    def knowledge(q: str = "", k: int = 8):
        e = eng()
        if q.strip():
            return [dict(c.to_dict(), score=round(s, 3)) for c, s in e.knowledge.search(q, k=k)]
        return [c.to_dict() for c in e.knowledge.chunks[:200]]

    @app.post("/api/knowledge/upload")
    async def upload(file: UploadFile):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in UPLOAD_TYPES:
            raise HTTPException(400, f"Supported uploads: {', '.join(sorted(UPLOAD_TYPES))}")
        folder = settings.path("runs/uploads")
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(file.filename).name)
        path = folder / safe
        path.write_bytes(await file.read())
        try:
            added = eng().knowledge.add_path(path)
        except (ValueError, KeyError) as error:
            raise HTTPException(400, f"Could not parse {safe}: {error}")
        eng()._table_cache.clear()
        return dict(file=safe, chunks=added)

    @app.post("/api/reload")
    def reload():
        with lock:
            state["engine"] = build_engine(settings)
        return status()

    return app
