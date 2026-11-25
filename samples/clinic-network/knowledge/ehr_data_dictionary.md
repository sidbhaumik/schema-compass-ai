# Legacy EHR data dictionary (excerpt)

## Naming
Columns use short clinical abbreviations: PT = patient, ENC/ENCNTR = encounter, DX = diagnosis, PROV = provider, FAC = facility, RSLT = result, DISCH = discharge, ATTND = attending, SPCLTY = specialty, APPT = appointment.

## Dates
BIRTH_DT is stored as text in YYYYMMDD format for historical reasons. Convert with TO_DATE before loading into a DATE column.

## Diagnoses
DX_CD holds ICD-9 codes before October 2015 and ICD-10 codes after. The target DIAGNOSIS table is ICD-10 only, so filter on DX_CD_SYS = 'ICD10' or apply the general equivalence mapping.

## Lab results
RSLT_VAL is free text. Most values are numeric, but some contain qualifiers such as <5 or >1000, or words like POSITIVE. These cannot be cast to a number without a cleansing rule.

## Length of stay
The EHR never stored length of stay. It is calculated as DISCH_DT minus ADMIT_DT, so LENGTH_OF_STAY_DAYS has no direct source column.
