"""Hybrid lexical + dense similarity over catalog entities."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .catalog import Column, Table
from .text import normalize


def label_text(item: Table | Column) -> str:
    # Synonym expansion helps short column names but drowns table names that share an
    # entity word ("Customer Orders" vs "Customer Master"), so tables use plain expansion.
    synonyms = isinstance(item, Column)
    name = item.short_name if isinstance(item, Table) else item.name
    return f"{normalize(name, synonyms)} {normalize(item.logical_name, synonyms)}".strip()


def full_text(item: Table | Column) -> str:
    return f"{label_text(item)} {normalize(item.description, synonyms=False)}".strip()


def readable_text(item: Table | Column) -> str:
    """Natural-language rendering used for dense embeddings and LLM prompts."""
    name = item.short_name if isinstance(item, Table) else item.name
    parts = [item.logical_name or normalize(name, synonyms=False), f"({name})"]
    if isinstance(item, Column) and item.data_type:
        parts.append(f"type {item.data_type}")
    if item.description:
        parts.append(f"- {item.description}")
    return " ".join(parts)


class Embedder:
    """Sentence-Transformer wrapper with an on-disk vector cache keyed by text hash."""

    def __init__(self, model_name: str, cache_dir: str | Path | None = None):
        from sentence_transformers import SentenceTransformer  # optional dependency

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.cache_path = None
        self.cache: dict[str, list[float]] = {}
        if cache_dir:
            slug = hashlib.sha1(model_name.encode()).hexdigest()[:10]
            self.cache_path = Path(cache_dir) / f"embeddings-{slug}.json"
            if self.cache_path.exists():
                self.cache = json.loads(self.cache_path.read_text())

    def encode(self, texts: list[str]) -> np.ndarray:
        keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
        missing = sorted({(k, t) for k, t in zip(keys, texts) if k not in self.cache})
        if missing:
            vectors = self.model.encode([t for _, t in missing], normalize_embeddings=True,
                                        batch_size=64)
            for (key, _), vector in zip(missing, vectors):
                self.cache[key] = vector.tolist()
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self.cache))
        return np.array([self.cache[k] for k in keys], dtype=np.float32)


class SimilarityModel:
    """Fits shared vocabularies on both catalogs so scores are comparable across them."""

    def __init__(self, corpus: list[Table | Column], embedder: Embedder | None = None):
        labels = [label_text(i) for i in corpus] or ["empty"]
        fulls = [full_text(i) for i in corpus] or ["empty"]
        self.label_chars = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                           sublinear_tf=True).fit(labels)
        self.label_words = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b", sublinear_tf=True).fit(labels)
        self.full_words = TfidfVectorizer(ngram_range=(1, 2), token_pattern=r"(?u)\b\w+\b",
                                          sublinear_tf=True).fit(fulls)
        self.embedder = embedder

    def encode(self, items: list[Table | Column]) -> dict:
        labels = [label_text(i) for i in items]
        fulls = [full_text(i) for i in items]
        encoded = dict(chars=self.label_chars.transform(labels),
                       words=self.label_words.transform(labels),
                       full=self.full_words.transform(fulls), dense=None)
        if self.embedder is not None and items:
            encoded["dense"] = self.embedder.encode([readable_text(i) for i in items])
        return encoded

    @staticmethod
    def subset(encoded: dict, rows: list[int]) -> dict:
        return {k: (None if v is None else v[rows]) for k, v in encoded.items()}

    @staticmethod
    def compare(query: dict, pool: dict) -> dict[str, np.ndarray]:
        """Return feature arrays of shape (len(pool),) for a single encoded query row."""
        label = 0.5 * (query["chars"] @ pool["chars"].T).toarray()[0] \
            + 0.5 * (query["words"] @ pool["words"].T).toarray()[0]
        full = (query["full"] @ pool["full"].T).toarray()[0]
        result = dict(label=np.clip(label, 0, 1), description=np.clip(full, 0, 1), semantic=None)
        if query["dense"] is not None and pool["dense"] is not None:
            result["semantic"] = np.clip(pool["dense"] @ query["dense"][0], 0, 1)
        return result
