# Clinic network EHR

Terse clinical codes in a legacy Databricks EHR mapped to a Snowflake warehouse.

A network of outpatient clinics is consolidating its legacy electronic health record extracts (Databricks) into a Snowflake clinical warehouse. All patients, clinicians and clinics are fictional; the metadata contains no patient data.

**What it exercises:** clinical abbreviations (PT, ENC, DX, RSLT), Excel S2T sheet, text-to-date and text-to-number conversions, derived field with no source.

## Files

| File | Contents |
| --- | --- |
| `source_metadata.csv` | Source metadata (Databricks: legacy_ehr.clinical, scheduling, reference (7 tables, short clinical codes)) |
| `target_metadata.csv` | Target metadata (Snowflake: HEALTH_DW.CLINICAL (7 tables)) |
| `knowledge/ehr_data_dictionary.md` | Reference notes |
| `knowledge/s2t_wave1_patient.xlsx` | S2T mapping sheet |
| `knowledge/slack_clinical_data.json` | Slack channel export |
| `gold_mappings.csv` | Answer key: the correct source for every target table and column (blank = no source) |
| `bulk_fields.csv` | A ready-made list for the **Bulk mapping** upload |

The answer key covers 7 tables and 41 columns, including 1 target column with no source.

## Try these searches

| Target table | Target column | What you'll see |
| --- | --- | --- |
| `HEALTH_DW.CLINICAL.PATIENT` | `DATE_OF_BIRTH` | BIRTH_DT, with a text-to-date conversion from the Excel S2T sheet. |
| `HEALTH_DW.CLINICAL.LAB_RESULT` | `RESULT_VALUE` | RSLT_VAL, with a warning that values like <5 need cleansing. |
| `HEALTH_DW.CLINICAL.ENCOUNTER` | `ATTENDING_PROVIDER_ID` | ATTND_PROV_ID through clinical abbreviations. |
| `HEALTH_DW.CLINICAL.DIAGNOSIS` | `ICD10_CODE` | DX_CD, with the ICD-9/ICD-10 caveat in the dictionary. |
| `HEALTH_DW.CLINICAL.APPOINTMENT` | `IS_NO_SHOW` | Derived from APPT_STAT_CD per the S2T sheet. |
| `HEALTH_DW.CLINICAL.ENCOUNTER` | `LENGTH_OF_STAY_DAYS` | No source: it is calculated. |

## Use it

- **Quickest:** in the app, select **New**, then **Create** next to "Clinic network EHR". The project is set up with all files loaded.
- **Practice the upload flow:** create an empty project and upload `source_metadata.csv`, `target_metadata.csv` and the files in `knowledge/` yourself in **Project setup**. Pick **Databricks** as the source platform and **Snowflake** as the target platform.
- **Check accuracy** against the answer key: `python -m schema_compass.cli evaluate --gold samples/clinic-network/gold_mappings.csv` (after pointing `schema-compass.toml` at this sample's files).

All organizations, people, products and conversations in this sample are fictional, and it contains metadata only, no data records.
