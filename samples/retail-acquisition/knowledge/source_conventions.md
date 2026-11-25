# Source warehouse conventions

## Naming
Physical names follow a six-letter abbreviation standard: NBR for number, CD for code, DT for date, TS for timestamp, AMT for amount, QTY for quantity, FLG for Y/N flags. Logical names in the data dictionary are maintained by the CRM and merchandising teams and may lag behind the physical model.

## Dates and time zones
ORD_DT and RTN_DT are business dates in store local time. Columns ending in TS are stored in UTC without a zone (TIMESTAMP_NTZ). Convert before comparing with target timestamps.

## Flags
Every column ending in FLG is a single-character Y/N value. Target booleans need an explicit CASE mapping, and nulls are treated as N unless the steward says otherwise.

## History
ORD_HDR holds orders from 2019 onward. Earlier orders live in ORD_HDR_LGCY, which uses different status codes (four characters) and has no channel or currency columns.
