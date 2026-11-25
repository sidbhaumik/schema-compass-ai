# Retail acquisition

An acquired retailer's terse Snowflake warehouse mapped to Databricks and Snowflake data products.

A retailer acquired another chain and is backfilling ten years of history. The acquired company's Snowflake warehouse uses abbreviated names (ORD_HDR.TOT_AMT); the target data products are spread across Databricks and Snowflake.

**What it exercises:** abbreviated names, deprecated look-alike table, S2T sheet and Slack evidence, type and width checks, table with no source.

## Files

| File | Contents |
| --- | --- |
| `source_metadata.csv` | Source metadata (Snowflake: SRC_DW (12 tables, abbreviated names)) |
| `target_metadata.csv` | Target metadata (Databricks + Snowflake: lumen, LUMEN_DW (10 tables)) |
| `knowledge/s2t_wave1_customer.csv` | S2T mapping sheet |
| `knowledge/slack_data_migration.json` | Slack channel export |
| `knowledge/source_conventions.md` | Reference notes |
| `gold_mappings.csv` | Answer key: the correct source for every target table and column (blank = no source) |
| `bulk_fields.csv` | A ready-made list for the **Bulk mapping** upload |

The answer key covers 10 tables and 72 columns, including 6 target columns with no source.

## Try these searches

| Target table | Target column | What you'll see |
| --- | --- | --- |
| `lumen.sales.orders` | *(blank)* | ORD_HDR wins; the deprecated ORD_HDR_LGCY is demoted. |
| `lumen.sales.orders` | `order_total_amount` | TOT_AMT, confirmed in Slack. |
| `LUMEN_DW.CUSTOMER.CUSTOMER_PROFILE` | `SIGNUP_DATE` | CRT_DT: names differ, an S2T sheet maps it. |
| `lumen.product.product_catalog` | `product_name` | ITEM_DESC, not SKU_ID or ITEM_LONG_DESC. |
| `LUMEN_DW.CUSTOMER.CUSTOMER_PROFILE` | `MARKETING_OPT_IN` | OPT_IN_FLG with a Y/N to boolean check. |
| `lumen.marketing.customer_reviews` | *(blank)* | No source: the reviews feed never existed. |

## Use it

- **Quickest:** in the app, select **New**, then **Create** next to "Retail acquisition". The project is set up with all files loaded.
- **Practice the upload flow:** create an empty project and upload `source_metadata.csv`, `target_metadata.csv` and the files in `knowledge/` yourself in **Project setup**. Pick **Snowflake** as the source platform and **Databricks** as the target platform.
- **Check accuracy** against the answer key: `python -m schema_compass.cli evaluate --gold samples/retail-acquisition/gold_mappings.csv` (after pointing `schema-compass.toml` at this sample's files).

All organizations, people, products and conversations in this sample are fictional, and it contains metadata only, no data records.
