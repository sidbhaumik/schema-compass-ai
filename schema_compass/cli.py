"""Command line interface: serve, match, map, evaluate, export reviews, harvest metadata."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from .app import build_engine, load_settings

DEFAULT_CONFIG = "schema-compass.toml"


def _print_result(result: dict):
    rec = result["recommendation"]
    target = result["target"]
    name = target["table"]["name"] + (f".{target['column']['name']}" if target["column"] else "")
    print(f"\n{name}\n  {rec['band'].upper()} {rec['confidence']:.1f} - {rec['text']}")
    if rec.get("summary"):
        print(f"  AI review: {rec['summary']}")
    for warning in result.get("warnings", []):
        print(f"  ! {warning['summary']}")
    for cand in result["candidates"]:
        ref = cand["source_table"]["name"] + (f".{cand['source_column']['name']}" if cand["source_column"] else "")
        print(f"  {cand['rank']}. {ref:<45} {cand['score']:>5.1f}  [{cand['band']}]")
        for line in cand["reasons"] + [f"! {i}" for i in cand["issues"]]:
            print(f"       {line}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="schema-compass", description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to schema-compass.toml")
    parser.add_argument("--embedding-model", help="Sentence-Transformer model for dense similarity")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the web UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    match = sub.add_parser("match", help="Match a target table or table.column")
    match.add_argument("table")
    match.add_argument("column", nargs="?")
    match.add_argument("--platform", choices=["SNOWFLAKE", "DATABRICKS"])
    match.add_argument("-k", type=int, default=5)
    match.add_argument("--rerank", action="store_true", help="Ask Claude to review the shortlist")
    match.add_argument("--json", action="store_true")

    mapping = sub.add_parser("map", help="Draft an S2T sheet for every column of a target table")
    mapping.add_argument("table")
    mapping.add_argument("--platform", choices=["SNOWFLAKE", "DATABRICKS"])
    mapping.add_argument("--output", type=Path, help="CSV path (default: stdout summary)")
    mapping.add_argument("--rerank", action="store_true")

    evaluate = sub.add_parser("evaluate", help="Score the matcher against gold labels")
    evaluate.add_argument("--gold", type=Path)
    evaluate.add_argument("--no-knowledge", action="store_true", help="Disable RAG evidence (ablation)")
    evaluate.add_argument("-k", type=int, default=3)

    export = sub.add_parser("export-approved", help="Write approved reviews as an S2T sheet")
    export.add_argument("output", type=Path)

    harvest = sub.add_parser("harvest-databricks", help="Export Unity Catalog metadata to the CSV contract")
    harvest.add_argument("--profile", required=True, help="Databricks CLI profile")
    harvest.add_argument("--catalog", action="append", required=True)
    harvest.add_argument("--schema", action="append")
    harvest.add_argument("--warehouse-id")
    harvest.add_argument("output", type=Path)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.command == "harvest-databricks":
        from .catalog import FIELDS
        from .connectors import databricks_rows

        rows = list(databricks_rows(args.catalog, profile=args.profile, warehouse_id=args.warehouse_id,
                                    schemas=args.schema))
        with open(args.output, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} column rows to {args.output}")
        return

    settings = load_settings(args.config)
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings, build_engine(settings, embedding_model=args.embedding_model)),
                    host=args.host, port=args.port)
        return

    if args.command == "evaluate":
        from .evaluate import evaluate as run_eval

        gold = args.gold or settings.path(settings.gold)
        if not gold:
            parser.error("Pass --gold or set [evaluation] gold in the config.")
        engine = build_engine(settings, use_llm=False, embedding_model=args.embedding_model,
                              with_knowledge=not args.no_knowledge, with_feedback=False)
        print(json.dumps(run_eval(engine, gold, k=args.k), indent=2))
        return

    if args.command == "export-approved":
        from .feedback import FeedbackStore

        count = FeedbackStore(settings.path(settings.feedback_db)).export_s2t(args.output)
        print(f"Wrote {count} approved mappings to {args.output}")
        return

    engine = build_engine(settings, use_llm=getattr(args, "rerank", False), embedding_model=args.embedding_model)
    if getattr(args, "rerank", False) and engine.reranker is None:
        parser.exit(1, "AI review needs Anthropic credentials (ANTHROPIC_API_KEY or `ant auth login`).\n")
    try:
        table = engine.target.find_table(args.table, args.platform)
    except ValueError as error:
        parser.exit(1, f"Error: {error}\n")

    if args.command == "match":
        if args.column:
            column = table.column(args.column)
            if column is None:
                parser.exit(1, f"Error: column {args.column!r} is not in {table.name}\n")
            result = engine.match_column(table, column, k=args.k, rerank=args.rerank)
        else:
            result = engine.match_table(table, k=args.k, rerank=args.rerank)
        print(json.dumps(result, indent=2, default=str) if args.json else "", end="")
        if not args.json:
            _print_result(result)
        return

    if args.command == "map":
        result = engine.map_table(table, rerank=args.rerank)
        if args.output:
            from fastapi.testclient import TestClient  # reuse the CSV writer in the API

            from .api import create_app

            client = TestClient(create_app(settings, engine))
            response = client.get("/api/map-table.csv", params=dict(table=table.id))
            args.output.write_text(response.text, encoding="utf-8")
            print(f"Wrote {len(result['columns'])} rows to {args.output}")
        else:
            _print_result(result["table"])
            for col in result["columns"]:
                top = col["candidates"][0] if col["candidates"] else None
                ref = f"{top['source_table']['name']}.{top['source_column']['name']}" if top else "-"
                print(f"  {col['target']['column']['name']:<24} <- {ref:<45} "
                      f"{col['recommendation']['confidence']:>5.1f} {col['recommendation']['band']}")


if __name__ == "__main__":
    sys.exit(main())
