# Schema Compass

Find the right **source table and column** for every field of a **target data product** during a migration, with a confidence score, an explanation, and the evidence behind it.

Give it a target table (Snowflake or Databricks), or a table and a column. Schema Compass searches the source organization's metadata semantically, pulls in tribal knowledge from prior source-to-target (S2T) sheets, Slack threads and notes, and recommends the best match. You can also ask Claude to review the shortlist. Analysts approve or reject suggestions, and each decision improves later matches.

```mermaid
flowchart LR
    S[Source metadata<br/>Snowflake / CSV] --> I[Hybrid index<br/>names · descriptions · embeddings]
    T[Target metadata<br/>Snowflake + Databricks UC] --> I
    I --> R[Retrieve<br/>table shortlist + global column search]
    K[Knowledge base<br/>S2T sheets · Slack · notes] --> E[Evidence]
    F[SME decisions] --> E
    R --> SC[Score<br/>similarity · table context · type · role]
    E --> SC
    SC --> L{AI review?}
    L -- optional --> C[Claude reranker<br/>verdict · rationale · load rule]
    L --> O[Ranked candidates<br/>confidence band · issues · evidence]
    C --> O
    O --> UI[Web UI / API / CLI / S2T CSV]
    UI -- approve / reject --> F
```

## Quick start

Requires Python 3.11 or later.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m schema_compass.cli serve
```

Open http://127.0.0.1:8000. The bundled demo is a fictional retail acquisition. **OrgTRGT** ("lumen", with Databricks and Snowflake products) is migrating ten years of history from **OrgSRC**, a Snowflake warehouse with terse names such as `ORD_HDR.TOT_AMT` and `CUST_MSTR.CRT_DT`. Try these:

| Search | What it shows |
| --- | --- |
| `orders` | `ORD_HDR` wins. The look-alike `ORD_HDR_LGCY` is demoted because a Slack thread calls it deprecated. |
| `orders` / `customer_id` | `ORD_HDR.CUST_ID`, not `CUST_MSTR.CUST_ID`. The parent table's context breaks the tie between identical column names. |
| `CUSTOMER_PROFILE` / `SIGNUP_DATE` | `CRT_DT`. The name barely matches, but a prior S2T sheet maps it. |
| `product_catalog` / `product_name` | `ITEM_DESC` is the match, not `SKU_ID` (an identifier, not a name) and not `ITEM_LONG_DESC` (Slack says it holds marketing copy). |
| `CUSTOMER_PROFILE` / `MARKETING_OPT_IN` | Flags the `VARCHAR(1)` to `BOOLEAN` conversion and shows the S2T `CASE` rule. |
| `customer_reviews` | Returns **No confident match**. The knowledge base says this source never existed. |
| **Map table** on any target | Drafts a full S2T sheet (downloadable CSV) with alternatives, issues and evidence. |

## What goes into a score

| Signal | What it captures |
| --- | --- |
| Name similarity | Physical and logical names after abbreviation expansion (`CUST_NM` becomes customer name), plural folding and business synonyms. Uses character and word TF-IDF. |
| Description similarity | Word n-gram TF-IDF over names plus descriptions |
| Semantic similarity *(optional)* | Sentence-transformer embeddings, cached on disk |
| Table context | A column inherits the strength of its parent table's match. A table match blends name similarity with **column coverage**: how well the target's columns are covered by the source table's columns. |
| Datatype compatibility | Same family, castable (with a note such as "decimal to integer may truncate"), or conflicting (capped) |
| Field role | Identifier vs name vs amount vs date vs flag, so `product_name` does not map to `SKU_ID` |
| Width | Flags a source `VARCHAR` wider than the target's |
| Evidence (RAG) | S2T rows (strong), Slack and notes passages that endorse or warn against a candidate, and "no source exists" statements |
| SME feedback | Approved pairs are pinned near 100. Rejected pairs are suppressed. |
| Claude review *(optional)* | Claude reads the shortlist and retrieved passages. It returns a verdict, confidence, rationale and load rule for each candidate, and is blended at 60% with the heuristic score. |

Confidence bands: **high** (80 and up, with a clear lead) means quick SME confirmation. **Medium** (60 to 79) means standard review. **Low** (40 to 59) means research before use. **None** (under 40) means likely a new, derived or out-of-scope field.

## Use your own metadata

Both organizations provide one row per column, using this contract. Table-level fields repeat on each row.

`PLATFORM, TABLE_PHYSICAL_NAME, TABLE_LOGICAL_NAME, TABLE_DESCRIPTION, COLUMN_PHYSICAL_NAME, COLUMN_LOGICAL_NAME, COLUMN_DESCRIPTION, COLUMN_DATATYPE, COLUMN_LENGTH`

Point [`schema-compass.toml`](schema-compass.toml) at CSV files, a Snowflake export table, or Unity Catalog directly:

```toml
[source]
type = "snowflake"
connection = "my_named_connection"     # from ~/.snowflake/connections.toml
table = "MIGRATION.META.SOURCE_COLUMNS"

[target]
type = "databricks"
profile = "my-profile"                 # Databricks CLI profile
catalogs = ["lumen"]
warehouse_id = "abc123"
```

Install the connector extras with `pip install -r requirements-connectors.txt`. To snapshot Unity Catalog metadata to the CSV contract:

```bash
python -m schema_compass.cli harvest-databricks --profile my-profile --catalog lumen target.csv
```

Logical names usually live in a data dictionary or governance tags rather than in `information_schema`. Fill them in when you have them, because they are among the strongest signals.

## Knowledge base (RAG)

Add files to the `[knowledge] paths` folders, or upload them on the **Knowledge** tab:

- **S2T sheets** (`.csv` / `.xlsx`). Columns named like `target_table, target_column, source_table, source_column, transformation, notes`; common aliases are accepted.
- **Slack exports** (`.json`). A channel export, either a message list or `{"channel", "messages"}`. Messages are grouped by thread.
- **Notes** (`.md` / `.txt`). Chunked by heading.

Evidence works per pair. A passage counts for a candidate only if it names both the target and the source field, and phrases such as "deprecated", "not X" or "don't use" turn it into a warning. Approved decisions can be exported with `export-approved` as an S2T sheet for the next wave.

## Optional components

```bash
# Dense embeddings
pip install -r requirements-semantic.txt
python -m schema_compass.cli --embedding-model sentence-transformers/all-MiniLM-L6-v2 serve

# Claude review: set ANTHROPIC_API_KEY (or run `ant auth login`), then tick "AI review" in the UI
python -m schema_compass.cli match product_catalog product_name --rerank
```

The AI review sends only the target field, the top candidates' metadata and the retrieved snippets. It uses `claude-opus-5-5` by default; change the model and effort in `[llm]`. Server-side refusal fallback is enabled. If the API is unavailable, results fall back to the heuristic ranking.

## CLI

```bash
python -m schema_compass.cli match orders                     # table-level
python -m schema_compass.cli match orders order_total_amount  # column-level
python -m schema_compass.cli map shipments --output s2t_shipments.csv
python -m schema_compass.cli evaluate                         # vs demo/gold_mappings.csv
python -m schema_compass.cli evaluate --no-knowledge          # ablation without RAG
python -m schema_compass.cli export-approved approved_s2t.csv
```

## Measuring quality

`evaluate` reports top-1 accuracy, recall@k, and abstention accuracy (whether no-source targets are correctly left unmatched) at both the table and column level. On the 82 hand-labeled demo pairs:

| | Table top-1 | Column top-1 | Column recall@3 | Abstention (column) |
| --- | ---: | ---: | ---: | ---: |
| Metadata only | 9/9 | 66/66 | 66/66 | 5/6 |
| Metadata + knowledge base | 9/9 | 66/66 | 66/66 | 6/6 |

Weights and thresholds were tuned on this small synthetic fixture, so treat these numbers as a regression floor, not a benchmark. Before relying on bands for a real program, label 50 to 100 pairs from your own domain, including look-alike negatives and fields with no source. Then run `evaluate --gold your_labels.csv` and adjust `Weights` in [`engine.py`](schema_compass/engine.py).

## Scaling notes

Source tables and columns are vectorized once at startup. A table query scores every source table but deep-scores (column coverage plus evidence) only a shortlist of 15. A column query scores every source column, then reranks the union of the top tables' columns, the global top 25, and anything an S2T sheet names. This handles tens of thousands of columns in a single process. Beyond that, move the dense vectors to Databricks Vector Search or Snowflake Cortex Search and keep this scoring layer as the reranker.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/match` | `{table, column?, platform?, k, rerank}` returns a ranked result |
| `POST` | `/api/map-table` | Draft mapping for every column of a target table |
| `GET` | `/api/map-table.csv?table=` | Same, as a downloadable S2T sheet |
| `POST` / `GET` | `/api/feedback` | Record or list SME decisions |
| `GET` | `/api/knowledge?q=` | Search the knowledge base |
| `POST` | `/api/knowledge/upload` | Add an S2T sheet, Slack export or note |
| `GET` | `/api/status` | Catalog sizes, knowledge counts, optional features |

All organizations, products, people and conversations in `demo/` are fictional.
