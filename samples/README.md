# Sample datasets

Each folder is a complete, fictional migration scenario. It contains source and target metadata, reference documents (S2T sheets, Slack exports, notes), an answer key and a bulk-mapping list. Load any of them in the app with **New → Create**, or download the files from the app to practice the upload flow.

| Sample | Scenario | Highlights |
| --- | --- | --- |
| [Retail acquisition](retail-acquisition/) | An acquired retailer's terse Snowflake warehouse mapped to Databricks and Snowflake data products. | abbreviated names, deprecated look-alike table, S2T sheet and Slack evidence |
| [SaaS product analytics](saas-product-analytics/) | camelCase tracker tables in Snowflake mapped to a Databricks lakehouse. | camelCase identifiers, deprecated look-alike table, VARIANT to JSON string |
| [Clinic network EHR](clinic-network/) | Terse clinical codes in a legacy Databricks EHR mapped to a Snowflake warehouse. | clinical abbreviations (PT, ENC, DX, RSLT), Excel S2T sheet, text-to-date and text-to-number conversions |
| [People analytics (HR)](people-analytics/) | An HRIS export delivered as Excel with its own headers, mapped to a Databricks people-analytics model. | Excel metadata with non-standard headers, close look-alikes (original vs last hire date, legal vs preferred name), unit conversion (percent to fraction) |

To add your own sample, create a folder with the same files and a `sample.json` manifest (copy one of these), and it appears in the app automatically.
