"""FastAPI service and self-service web UI, organized by mapping project."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .export import bulk_requests, result_row, to_csv
from .workspace import Project, Workspace, read_table_file

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
MAX_AI_BULK = 50
USER_HEADERS = ("x-forwarded-email", "x-forwarded-preferred-username", "x-forwarded-user", "x-auth-request-email")


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


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
    reviewer: str = ""
    note: str = ""
    confidence: float | None = None


def current_user(request: Request) -> str | None:
    """Identity supplied by an SSO proxy or Databricks Apps, if any."""
    for header in USER_HEADERS:
        if request.headers.get(header):
            return request.headers[header]
    return None


async def read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 25 MB.")
    if not data:
        raise HTTPException(400, "The file is empty.")
    return data


def create_app(workspace: Workspace, *, embedder=None, reranker=None) -> FastAPI:
    app = FastAPI(title="Schema Compass", version=__version__)

    def project(pid: str) -> Project:
        try:
            return workspace.get(pid)
        except KeyError:
            raise HTTPException(404, "Project not found.")

    def engine(p: Project):
        try:
            return p.engine(embedder=embedder, reranker=reranker)
        except ValueError as error:
            raise HTTPException(409, str(error))

    def resolve(e, table: str, column: str | None, platform: str | None = None):
        try:
            target = e.target.find_table(table, platform or None)
        except ValueError as error:
            raise HTTPException(404, str(error))
        field = None
        if column and column.strip():
            field = target.column(column)
            if field is None:
                raise HTTPException(404, f"Column '{column}' is not in {target.name}.")
        return target, field

    def need_ai(rerank: bool):
        if rerank and reranker is None:
            raise HTTPException(400, "AI review is not configured on this server.")

    # ------------------------------------------------------------------ meta
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def health():
        return dict(status="ok", version=__version__, projects=len(workspace.projects()))

    @app.get("/api/config")
    def config(request: Request):
        return dict(version=__version__, user=current_user(request), ai_review=getattr(reranker, "model", None),
                    embeddings=getattr(embedder, "model_name", None), max_ai_bulk=MAX_AI_BULK)

    # -------------------------------------------------------------- projects
    @app.get("/api/projects")
    def list_projects():
        return [p.summary() for p in workspace.projects()]

    @app.post("/api/projects")
    def create_project(body: ProjectRequest):
        try:
            return workspace.create(body.name, body.description).summary()
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        project(pid)
        workspace.delete(pid)
        return dict(deleted=pid)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = project(pid)
        info = p.summary()
        if info["ready"]:
            e = engine(p)
            kinds: dict[str, int] = {}
            for chunk in e.knowledge.chunks:
                kinds[chunk.kind] = kinds.get(chunk.kind, 0) + 1
            decisions = e.feedback.latest() if e.feedback else []
            info["stats"] = dict(
                source_tables=len(e.source), source_columns=len(e.source.columns),
                target_tables=len(e.target), target_columns=len(e.target.columns),
                knowledge=kinds, s2t_rows=len(e.knowledge.mappings),
                approved=sum(d["decision"] == "approved" for d in decisions),
                rejected=sum(d["decision"] == "rejected" for d in decisions))
        return info

    @app.post("/api/projects/{pid}/metadata/{side}")
    async def upload_metadata(pid: str, side: str, file: UploadFile = File(...), platform: str = Form("SNOWFLAKE")):
        if side not in {"source", "target"}:
            raise HTTPException(404, "Unknown side.")
        p = project(pid)
        try:
            return p.import_metadata(side, file.filename or "metadata.csv", await read_upload(file),
                                     platform.upper())
        except (ValueError, KeyError) as error:
            raise HTTPException(400, str(error))

    # ------------------------------------------------------------- knowledge
    @app.get("/api/projects/{pid}/knowledge")
    def knowledge(pid: str, q: str = "", k: int = 8):
        e = engine(project(pid))
        if q.strip():
            return [dict(c.to_dict(), score=round(s, 3)) for c, s in e.knowledge.search(q, k=k)]
        return [c.to_dict() for c in e.knowledge.chunks[:200]]

    @app.post("/api/projects/{pid}/knowledge")
    async def upload_knowledge(pid: str, file: UploadFile = File(...)):
        try:
            return project(pid).add_knowledge(file.filename or "notes.txt", await read_upload(file))
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.delete("/api/projects/{pid}/knowledge/{name}")
    def delete_knowledge(pid: str, name: str):
        try:
            project(pid).remove_knowledge(name)
        except ValueError as error:
            raise HTTPException(404, str(error))
        return dict(deleted=name)

    # -------------------------------------------------------------- matching
    @app.get("/api/projects/{pid}/suggest")
    def suggest(pid: str, q: str = "", table: str = "", limit: int = 12):
        """Typeahead: tables, or columns of a chosen table."""
        e = engine(project(pid))
        term = q.strip().casefold()
        if table.strip():
            try:
                t = e.target.find_table(table)
            except ValueError:
                return []
            return [dict(kind="column", value=c.name, label=c.logical_name or "", detail=c.data_type or "")
                    for c in t.columns if not term or term in c.name.casefold()
                    or term in (c.logical_name or "").casefold()][:limit]
        hits = [t for t in e.target.tables if not term or term in t.name.casefold()
                or term in (t.logical_name or "").casefold()]
        hits.sort(key=lambda t: (not t.short_name.casefold().startswith(term), t.name))
        return [dict(kind="table", value=t.name, label=t.logical_name or "", detail=t.platform.title(),
                     columns=len(t.columns)) for t in hits[:limit]]

    @app.get("/api/projects/{pid}/table")
    def table(pid: str, name: str, side: str = "target"):
        e = engine(project(pid))
        catalog = e.target if side == "target" else e.source
        try:
            return catalog.find_table(name).to_dict(columns=True)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.post("/api/projects/{pid}/match")
    def match(pid: str, body: MatchRequest):
        need_ai(body.rerank)
        e = engine(project(pid))
        target, field = resolve(e, body.table, body.column, body.platform)
        if field is None:
            return e.match_table(target, k=body.k, rerank=body.rerank)
        return e.match_column(target, field, k=body.k, rerank=body.rerank)

    @app.post("/api/projects/{pid}/map-table")
    def map_table(pid: str, body: MatchRequest):
        need_ai(body.rerank)
        e = engine(project(pid))
        target, _ = resolve(e, body.table, None, body.platform)
        return e.map_table(target, k=3, rerank=body.rerank)

    @app.get("/api/projects/{pid}/map-table.csv")
    def map_table_csv(pid: str, table: str):
        e = engine(project(pid))
        target, _ = resolve(e, table, None)
        result = e.map_table(target, k=3)
        rows = [result_row(c) for c in result["columns"]]
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", target.name)
        return Response(to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="s2t_{name}.csv"'})

    @app.post("/api/projects/{pid}/bulk")
    async def bulk(pid: str, file: UploadFile = File(...), rerank: bool = Form(False)):
        """Match a list of target tables/columns. A blank column expands to every column of the table."""
        need_ai(rerank)
        p = project(pid)
        e = engine(p)
        try:
            pairs = bulk_requests(read_table_file(file.filename or "list.csv", await read_upload(file)))
        except (ValueError, KeyError) as error:
            raise HTTPException(400, str(error))
        rows, errors, jobs = [], [], []
        for table_name, column_name in pairs:
            try:
                target, field = resolve(e, table_name, column_name)
            except HTTPException as error:
                errors.append(dict(table=table_name, column=column_name, error=error.detail))
                continue
            jobs += [(target, field)] if field else [(target, c) for c in target.columns]
        if rerank and len(jobs) > MAX_AI_BULK:
            raise HTTPException(400, f"AI review is limited to {MAX_AI_BULK} columns per upload "
                                     f"({len(jobs)} requested). Split the file or turn AI review off.")
        if len(jobs) > 5000:
            raise HTTPException(400, "Bulk uploads are limited to 5,000 columns.")
        for target, field in jobs:
            rows.append(result_row(e.match_column(target, field, k=3, rerank=rerank)))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        exports = p.root / "exports"
        exports.mkdir(exist_ok=True)
        (exports / f"bulk-{stamp}.csv").write_text(to_csv(rows), encoding="utf-8")
        workspace.sync(exports / f"bulk-{stamp}.csv")
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["decision"]] = counts.get(row["decision"], 0) + 1
        return dict(export=f"bulk-{stamp}", rows=rows, errors=errors, counts=counts)

    @app.get("/api/projects/{pid}/exports/{name}.csv")
    def export(pid: str, name: str):
        if not re.fullmatch(r"bulk-\d{8}-\d{6}", name):
            raise HTTPException(404, "Export not found.")
        path = project(pid).root / "exports" / f"{name}.csv"
        if not path.exists():
            raise HTTPException(404, "Export not found.")
        return FileResponse(path, media_type="text/csv", filename=f"schema-compass-{name}.csv")

    # -------------------------------------------------------------- reviews
    @app.post("/api/projects/{pid}/feedback")
    def feedback(pid: str, body: FeedbackRequest, request: Request):
        e = engine(project(pid))
        data = body.model_dump()
        data["reviewer"] = current_user(request) or data["reviewer"]
        try:
            review_id = e.feedback.record(**data)
        except ValueError as error:
            raise HTTPException(400, str(error))
        e._table_cache.clear()
        return dict(id=review_id)

    @app.get("/api/projects/{pid}/feedback")
    def feedback_history(pid: str, limit: int = 200):
        return engine(project(pid)).feedback.history(limit)

    @app.get("/api/projects/{pid}/approved.csv")
    def approved_csv(pid: str):
        p = project(pid)
        e = engine(p)
        path = p.root / "exports" / "approved.csv"
        path.parent.mkdir(exist_ok=True)
        e.feedback.export_s2t(path)
        return FileResponse(path, media_type="text/csv", filename="approved_mappings.csv")

    return app

