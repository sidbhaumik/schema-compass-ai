"""Self-service projects: each mapping effort keeps its own metadata, documents and reviews.

Layout on disk (default ./workspace, override with SCHEMA_COMPASS_HOME):

    workspace/projects/<id>/project.json
                           source_metadata.csv      normalized to the metadata contract
                           target_metadata.csv
                           knowledge/                S2T sheets, Slack exports, notes
                           feedback.db               reviewer decisions
                           exports/                  bulk mapping results
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .catalog import FIELDS, Catalog
from .engine import MatchEngine
from .feedback import FeedbackStore
from .knowledge import KnowledgeBase

# Accepted header spellings for each contract field (compared after lowercasing and
# replacing non-alphanumerics with underscores).
HEADER_ALIASES = {
    "PLATFORM": ("platform", "system", "source_system", "target_system"),
    "TABLE_PHYSICAL_NAME": ("table_physical_name", "table_name", "table", "physical_table_name",
                            "fully_qualified_table_name", "table_fqn", "object_name"),
    "TABLE_LOGICAL_NAME": ("table_logical_name", "table_business_name", "logical_table_name", "entity_name",
                           "data_product", "data_product_name"),
    "TABLE_DESCRIPTION": ("table_description", "table_comment", "table_desc", "table_definition", "entity_description"),
    "COLUMN_PHYSICAL_NAME": ("column_physical_name", "column_name", "column", "field_name", "field",
                             "physical_column_name", "attribute_physical_name"),
    "COLUMN_LOGICAL_NAME": ("column_logical_name", "column_business_name", "business_name", "logical_name",
                            "attribute_name", "logical_column_name", "business_term"),
    "COLUMN_DESCRIPTION": ("column_description", "description", "column_comment", "definition", "comment",
                           "column_definition", "business_definition", "attribute_description"),
    "COLUMN_DATATYPE": ("column_datatype", "data_type", "datatype", "type", "column_type", "full_data_type"),
    "COLUMN_LENGTH": ("column_length", "length", "character_maximum_length", "max_length", "char_length", "size"),
}
QUALIFIERS = (("table_catalog", "catalog", "database", "database_name", "catalog_name"),
              ("table_schema", "schema", "schema_name"))
KNOWLEDGE_TYPES = {".csv", ".json", ".md", ".txt", ".xlsx"}
METADATA_TYPES = {".csv", ".xlsx"}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header or "").strip().lower()).strip("_")


def read_table_file(filename: str, data: bytes) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook  # optional dependency

        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = sheet.iter_rows(values_only=True)
        header = [str(h or "") for h in next(rows)]
        return [dict(zip(header, r)) for r in rows if any(v not in (None, "") for v in r)]
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def normalize_metadata(rows: list[dict], default_platform: str = "SNOWFLAKE") -> tuple[list[dict], dict]:
    """Map arbitrary metadata headers onto the contract and report data quality."""
    if not rows:
        raise ValueError("The file has no rows.")
    headers = {_key(h): h for h in rows[0].keys() if h is not None}
    mapping, missing = {}, []
    for field, aliases in HEADER_ALIASES.items():
        found = next((headers[a] for a in aliases if a in headers), None)
        if found is not None:
            mapping[field] = found
    qualifiers = [next((headers[a] for a in group if a in headers), None) for group in QUALIFIERS]
    for required in ("TABLE_PHYSICAL_NAME", "COLUMN_PHYSICAL_NAME"):
        if required not in mapping:
            missing.append(required)
    if missing:
        raise ValueError("Could not find a column for " + " and ".join(
            f"{m} (e.g. {HEADER_ALIASES[m][1]})" for m in missing)
            + f". Headers found: {', '.join(list(headers.values())[:15])}")
    out = []
    for row in rows:
        record = {field: (str(row.get(src)).strip() if row.get(src) not in (None, "") else "")
                  for field, src in mapping.items()}
        table = record.get("TABLE_PHYSICAL_NAME", "")
        if not table:
            continue
        prefix = [str(row.get(q)).strip() for q in qualifiers if q and row.get(q) not in (None, "")]
        if prefix and table.count(".") == 0:
            record["TABLE_PHYSICAL_NAME"] = ".".join(prefix + [table])
        record["PLATFORM"] = (record.get("PLATFORM") or default_platform).upper()
        if record["PLATFORM"] not in {"SNOWFLAKE", "DATABRICKS"}:
            record["PLATFORM"] = "DATABRICKS" if "databricks" in record["PLATFORM"].lower() else "SNOWFLAKE"
        length = record.get("COLUMN_LENGTH", "")
        try:
            record["COLUMN_LENGTH"] = str(int(float(length))) if length and float(length) > 0 else ""
        except ValueError:
            record["COLUMN_LENGTH"] = ""
        out.append({f: record.get(f, "") for f in FIELDS})
    catalog = Catalog.from_rows(out, "upload")  # validates duplicates and structure
    columns = catalog.columns
    share = lambda attr: round(100 * sum(1 for c in columns if getattr(c, attr)) / len(columns)) if columns else 0
    report = dict(tables=len(catalog), columns=len(columns), mapped_headers={k: str(v) for k, v in mapping.items()},
                  ignored_headers=[str(h) for h in headers.values() if h not in mapping.values()
                                   and h not in qualifiers],
                  with_description=share("description"), with_business_name=share("logical_name"),
                  with_datatype=share("data_type"),
                  platforms=sorted({t.platform for t in catalog.tables}))
    tips = []
    if report["with_description"] < 60:
        tips.append(f"Only {report['with_description']}% of columns have a description. Matches will lean on "
                    "names; adding descriptions improves accuracy the most.")
    if report["with_business_name"] < 50:
        tips.append("Few columns have a business (logical) name. Add them if your data dictionary has them.")
    if report["with_datatype"] < 80:
        tips.append("Some columns have no data type, so type compatibility cannot be checked for them.")
    report["tips"] = tips
    return out, report


def _noop(path: Path, deleted: bool = False):
    pass


class Project:
    def __init__(self, root: Path, sync=_noop):
        self.root = root
        self.sync = sync
        self.meta = json.loads((root / "project.json").read_text(encoding="utf-8"))
        self._engine: MatchEngine | None = None
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        return self.meta["id"]

    def path(self, side: str) -> Path:
        if side not in {"source", "target"}:
            raise ValueError("side must be source or target")
        return self.root / f"{side}_metadata.csv"

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    def save_meta(self):
        (self.root / "project.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        self.sync(self.root / "project.json")

    def summary(self) -> dict:
        info = dict(self.meta)
        for side in ("source", "target"):
            info[f"{side}_ready"] = self.path(side).exists()
            info[f"{side}_report"] = self.meta.get(f"{side}_report")
        info["knowledge_files"] = sorted(p.name for p in self.knowledge_dir.glob("*") if p.is_file())
        info["ready"] = info["source_ready"] and info["target_ready"]
        return info

    def import_metadata(self, side: str, filename: str, data: bytes, platform: str = "SNOWFLAKE") -> dict:
        if Path(filename).suffix.lower() not in METADATA_TYPES:
            raise ValueError("Upload metadata as .csv or .xlsx.")
        rows, report = normalize_metadata(read_table_file(filename, data), platform)
        with open(self.path(side), "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        self.sync(self.path(side))
        report.update(file=Path(filename).name, uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.meta[f"{side}_report"] = report
        self.save_meta()
        self.invalidate()
        return report

    def add_knowledge(self, filename: str, data: bytes) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in KNOWLEDGE_TYPES:
            raise ValueError(f"Supported documents: {', '.join(sorted(KNOWLEDGE_TYPES))}")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(filename).name)
        self.knowledge_dir.mkdir(exist_ok=True)
        target = self.knowledge_dir / safe
        target.write_bytes(data)
        probe = KnowledgeBase()
        try:
            chunks = probe.add_path(target)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            target.unlink(missing_ok=True)
            raise ValueError(f"Could not read {safe}: {error}") from error
        if chunks == 0:
            target.unlink(missing_ok=True)
            raise ValueError(f"No usable content found in {safe}. S2T sheets need target/source table and column headers.")
        self.sync(target)
        self.invalidate()
        return dict(file=safe, passages=chunks, mappings=len(probe.mappings))

    def remove_knowledge(self, filename: str):
        target = self.knowledge_dir / Path(filename).name
        if not target.is_file():
            raise ValueError("Document not found.")
        target.unlink()
        self.sync(target, deleted=True)
        self.invalidate()

    def invalidate(self):
        with self._lock:
            self._engine = None

    def engine(self, *, embedder=None, reranker=None) -> MatchEngine:
        with self._lock:
            if self._engine is None:
                if not (self.path("source").exists() and self.path("target").exists()):
                    raise ValueError("Upload both source and target metadata to start matching.")
                knowledge = KnowledgeBase()
                if self.knowledge_dir.exists():
                    knowledge.add_path(self.knowledge_dir)
                self._engine = MatchEngine(
                    Catalog.from_csv(self.path("source"), "source"), Catalog.from_csv(self.path("target"), "target"),
                    knowledge=knowledge,
                    feedback=FeedbackStore(self.root / "feedback.db", on_write=self.sync),
                    embedder=embedder, reranker=reranker)
            return self._engine


class Workspace:
    def __init__(self, home: str | Path, sync=None):
        """``sync(path, deleted=False)`` is called after every file change (e.g. a VolumeMirror)."""
        self.home = Path(home).expanduser().resolve()
        (self.home / "projects").mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, Project] = {}
        self._lock = threading.Lock()
        self.sync = sync or _noop

    def projects(self) -> list[Project]:
        found = []
        for meta in sorted((self.home / "projects").glob("*/project.json")):
            pid = meta.parent.name
            if pid not in self._projects:
                self._projects[pid] = Project(meta.parent, self.sync)
            if not self._projects[pid].meta.get("deleted_at"):
                found.append(self._projects[pid])
        return sorted(found, key=lambda p: p.meta.get("created_at", ""))

    def get(self, pid: str) -> Project:
        if not re.fullmatch(r"[a-z0-9-]{1,64}", pid or ""):
            raise KeyError(pid)
        if pid not in self._projects:
            root = self.home / "projects" / pid
            if not (root / "project.json").exists():
                raise KeyError(pid)
            self._projects[pid] = Project(root, self.sync)
        if self._projects[pid].meta.get("deleted_at"):
            raise KeyError(pid)
        return self._projects[pid]

    def create(self, name: str, description: str = "") -> Project:
        name = name.strip()
        if not name:
            raise ValueError("Project name is required.")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
        pid = f"{slug}-{uuid.uuid4().hex[:6]}"
        root = self.home / "projects" / pid
        (root / "knowledge").mkdir(parents=True)
        meta = dict(id=pid, name=name, description=description.strip(),
                    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        (root / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.sync(root / "project.json")
        return self.get(pid)

    def delete(self, pid: str):
        """Hide a project. Files are kept (and can be restored by removing ``deleted_at``)."""
        project = self.get(pid)
        with self._lock:
            project.meta["deleted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            project.save_meta()
            project.invalidate()

    def create_from_sample(self, sample_dir: Path, name: str | None = None) -> Project:
        """Create a ready-to-use project from a bundled sample folder."""
        manifest = load_sample(sample_dir)
        project = self.create(name or manifest["name"], manifest.get("scenario", ""))
        for side in ("source", "target"):
            path = sample_dir / manifest[f"{side}_file"]
            project.import_metadata(side, path.name, path.read_bytes(), manifest.get(f"{side}_platform", "SNOWFLAKE"))
        for doc in sorted((sample_dir / "knowledge").glob("*")):
            if doc.is_file():
                project.add_knowledge(doc.name, doc.read_bytes())
        project.meta.update(sample=manifest["id"], examples=manifest.get("examples", []))
        project.save_meta()
        return project

    def seed_demo(self, samples_dir: Path, sample_id: str = "retail-acquisition") -> Project | None:
        """Create the default sample project on first run (when the workspace is empty)."""
        sample = Path(samples_dir) / sample_id
        if self.projects() or not (sample / "sample.json").exists():
            return None
        return self.create_from_sample(sample)


def load_sample(sample_dir: Path) -> dict:
    manifest = json.loads((Path(sample_dir) / "sample.json").read_text(encoding="utf-8"))
    manifest.setdefault("id", Path(sample_dir).name)
    return manifest


def list_samples(samples_dir: Path) -> list[dict]:
    samples_dir = Path(samples_dir)
    if not samples_dir.exists():
        return []
    found = [load_sample(p.parent) for p in samples_dir.glob("*/sample.json")]
    return sorted(found, key=lambda m: (m.get("order", 99), m["name"]))
