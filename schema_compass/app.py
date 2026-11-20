"""Configuration and engine assembly shared by the CLI and the web service."""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .engine import MatchEngine
from .feedback import FeedbackStore
from .knowledge import KnowledgeBase

log = logging.getLogger(__name__)


@dataclass
class Settings:
    base: Path
    source: dict
    target: dict
    knowledge: list[str] = field(default_factory=list)
    feedback_db: str = "runs/feedback.db"
    embedding_model: str | None = None
    llm: dict = field(default_factory=dict)
    gold: str | None = None

    def path(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.base / p


def load_settings(path: str | Path) -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Settings(base=path.parent, source=data["source"], target=data["target"],
                    knowledge=data.get("knowledge", {}).get("paths", []),
                    feedback_db=data.get("feedback", {}).get("db", "runs/feedback.db"),
                    embedding_model=data.get("embeddings", {}).get("model"),
                    llm=data.get("llm", {}), gold=data.get("evaluation", {}).get("gold"))


def load_catalog(spec: dict, settings: Settings, label: str) -> Catalog:
    kind = spec.get("type", "csv")
    if kind == "csv":
        return Catalog.from_csv(settings.path(spec["path"]), label)
    if kind == "snowflake":
        return Catalog.from_snowflake(spec["connection"], spec["table"], label)
    if kind == "databricks":
        return Catalog.from_databricks(spec["catalogs"], label, profile=spec.get("profile"),
                                       warehouse_id=spec.get("warehouse_id"), schemas=spec.get("schemas"))
    raise ValueError(f"Unknown catalog type {kind!r} for {label}")


def build_engine(settings: Settings, *, use_llm: bool | None = None, embedding_model: str | None = None,
                 with_knowledge: bool = True, with_feedback: bool = True) -> MatchEngine:
    source = load_catalog(settings.source, settings, "source")
    target = load_catalog(settings.target, settings, "target")
    knowledge = KnowledgeBase()
    if with_knowledge:
        for entry in settings.knowledge:
            p = settings.path(entry)
            if p and p.exists():
                knowledge.add_path(p)
            else:
                log.warning("Knowledge path not found: %s", p)
        uploads = settings.path("runs/uploads")
        if uploads.exists():
            knowledge.add_path(uploads)
    feedback = FeedbackStore(settings.path(settings.feedback_db)) if with_feedback else None
    embedder = None
    model_name = embedding_model or settings.embedding_model
    if model_name:
        from .index import Embedder

        embedder = Embedder(model_name, cache_dir=settings.path("runs/cache"))
    reranker = None
    if use_llm if use_llm is not None else settings.llm.get("enabled", True):
        from .rerank import reranker_from_env

        reranker = reranker_from_env(model=settings.llm.get("model"), effort=settings.llm.get("effort", "medium"))
    return MatchEngine(source, target, knowledge=knowledge, feedback=feedback, embedder=embedder,
                       reranker=reranker)
