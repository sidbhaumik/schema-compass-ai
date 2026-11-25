# People analytics (HR)

An HRIS export delivered as Excel with its own headers, mapped to a Databricks people-analytics model.

An HR analytics team is moving workforce reporting to Databricks. The HRIS team delivered the source metadata as an Excel workbook with their own column headers (Table Name, Field Name, Definition, Data Type).

**What it exercises:** Excel metadata with non-standard headers, close look-alikes (original vs last hire date, legal vs preferred name), unit conversion (percent to fraction), derived and no-source fields.

## Files

| File | Contents |
| --- | --- |
| `source_metadata.xlsx` | Source metadata (Snowflake HRIS (5 tables), delivered as source_metadata.xlsx) |
| `target_metadata.csv` | Target metadata (Databricks: people.analytics, people.recruiting (5 tables)) |
| `knowledge/hris_glossary.md` | Reference notes |
| `knowledge/slack_people_data.json` | Slack channel export |
| `gold_mappings.csv` | Answer key: the correct source for every target table and column (blank = no source) |
| `bulk_fields.csv` | A ready-made list for the **Bulk mapping** upload |

The answer key covers 5 tables and 39 columns, including 2 target columns with no source.

## Try these searches

| Target table | Target column | What you'll see |
| --- | --- | --- |
| `people.analytics.employees` | `hire_date` | LAST_HIRE_DT beats ORIG_HIRE_DT because of a Slack decision. |
| `people.analytics.employees` | `first_name` | PREF_FIRST_NM, not LEGAL_FIRST_NM. |
| `people.analytics.employees` | `fte` | FTE_PCT with a percent-to-fraction rule. |
| `people.analytics.job_profiles` | `overtime_eligible` | Derived from FLSA_STAT, explained in the glossary. |
| `people.analytics.employees` | `manager_id` | MGR_WORKER_ID through abbreviation expansion. |
| `people.analytics.employees` | `engagement_score` | No source: surveys never fed the HRIS. |

## Use it

- **Quickest:** in the app, select **New**, then **Create** next to "People analytics (HR)". The project is set up with all files loaded.
- **Practice the upload flow:** create an empty project and upload `source_metadata.xlsx`, `target_metadata.csv` and the files in `knowledge/` yourself in **Project setup**. Pick **Snowflake** as the source platform and **Databricks** as the target platform.
- **Check accuracy** against the answer key: `python -m schema_compass.cli evaluate --gold samples/people-analytics/gold_mappings.csv` (after pointing `schema-compass.toml` at this sample's files).

All organizations, people, products and conversations in this sample are fictional, and it contains metadata only, no data records.
