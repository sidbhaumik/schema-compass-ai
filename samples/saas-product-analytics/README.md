# SaaS product analytics

camelCase tracker tables in Snowflake mapped to a Databricks lakehouse.

A software company is rebuilding its analytics on Databricks. Usage events, accounts, users, experiments and support tickets come from a Snowflake warehouse whose tables were named by app developers in camelCase.

**What it exercises:** camelCase identifiers, deprecated look-alike table, VARIANT to JSON string, timestamp to date, fields with no source.

## Files

| File | Contents |
| --- | --- |
| `source_metadata.csv` | Source metadata (Snowflake: RAW.EVENTS, CORE, PRODUCT, SUPPORT (7 tables, camelCase)) |
| `target_metadata.csv` | Target metadata (Databricks: insight.product, insight.support (6 tables)) |
| `knowledge/s2t_experiments.csv` | S2T mapping sheet |
| `knowledge/slack_analytics_migration.json` | Slack channel export |
| `knowledge/tracker_conventions.md` | Reference notes |
| `gold_mappings.csv` | Answer key: the correct source for every target table and column (blank = no source) |
| `bulk_fields.csv` | A ready-made list for the **Bulk mapping** upload |

The answer key covers 6 tables and 39 columns, including 5 target columns with no source.

## Try these searches

| Target table | Target column | What you'll see |
| --- | --- | --- |
| `insight.product.events` | *(blank)* | trackEvents wins; trackEvents_v1 is flagged as deprecated in Slack. |
| `insight.product.events` | `event_timestamp` | evtTs, matched through the Ts = timestamp convention. |
| `insight.product.experiment_exposures` | `experiment_key` | flagKey: different words, confirmed by an S2T sheet. |
| `insight.product.accounts` | `signup_date` | signupTs with a timestamp-to-date check. |
| `insight.product.events` | `properties` | props: VARIANT loaded as a JSON string. |
| `insight.product.nps_responses` | *(blank)* | No source: the survey tool was never synced. |

## Use it

- **Quickest:** in the app, select **New**, then **Create** next to "SaaS product analytics". The project is set up with all files loaded.
- **Practice the upload flow:** create an empty project and upload `source_metadata.csv`, `target_metadata.csv` and the files in `knowledge/` yourself in **Project setup**. Pick **Snowflake** as the source platform and **Databricks** as the target platform.
- **Check accuracy** against the answer key: `python -m schema_compass.cli evaluate --gold samples/saas-product-analytics/gold_mappings.csv` (after pointing `schema-compass.toml` at this sample's files).

All organizations, people, products and conversations in this sample are fictional, and it contains metadata only, no data records.
