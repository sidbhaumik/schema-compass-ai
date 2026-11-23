# Changelog

## 1.0.0 (2026-11-22)

First public release.

### Matching
- Hybrid matching of target tables and columns against source metadata. It combines abbreviation-aware name similarity, description similarity and optional sentence-transformer embeddings.
- Each match is scored on the parent table's fit, data type compatibility, field role (identifier vs name vs amount) and text width.
- Evidence from team knowledge: S2T sheets (`.csv`, `.xlsx`), Slack channel exports (`.json`) and notes (`.md`, `.txt`), including warnings and "no source exists" statements.
- Reviewer decisions feed back into ranking: approvals are pinned, rejections suppressed.
- Optional AI review with Claude adds a verdict, rationale and suggested load rule for each candidate.

### Self-service web app
- Projects created in the browser, with source and target metadata uploaded as CSV or Excel. Headers are detected flexibly, and each upload gets a data-quality report.
- A decision card for each lookup: verdict, 0–100 confidence, plain-language reasons, checks before use, and a "How we found this" trace.
- Bulk mapping for a whole table or an uploaded list of fields, with a spreadsheet export.
- A decisions log, with export of approved mappings as an S2T sheet.
- The reviewer is filled in automatically from SSO or Databricks Apps identity headers.

### Deployment
- Docker image with a health check and a persistent `/data` volume, published to `ghcr.io/sidbhaumik/schema-compass-ai`.
- Databricks Apps support, with projects persisted on a Unity Catalog volume.
- CLI for matching, S2T drafts, evaluation against gold labels and Unity Catalog metadata harvesting.

### Known limitations
- No built-in login. Run locally or behind an identity-aware proxy or Databricks Apps.
- Scoring weights were tuned on the bundled synthetic demo. Validate on labeled pairs from your own migration before relying on the confidence bands.
- Volume-backed storage assumes a single app instance.
