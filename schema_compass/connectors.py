"""Metadata harvesting from live platforms. All dependencies here are optional."""
from __future__ import annotations

import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DATABRICKS_SQL = """
SELECT 'DATABRICKS'                                   AS PLATFORM,
       concat_ws('.', c.table_catalog, c.table_schema, c.table_name) AS TABLE_PHYSICAL_NAME,
       NULL                                           AS TABLE_LOGICAL_NAME,
       t.comment                                      AS TABLE_DESCRIPTION,
       c.column_name                                  AS COLUMN_PHYSICAL_NAME,
       NULL                                           AS COLUMN_LOGICAL_NAME,
       c.comment                                      AS COLUMN_DESCRIPTION,
       c.full_data_type                               AS COLUMN_DATATYPE,
       c.character_maximum_length                     AS COLUMN_LENGTH
FROM {catalog}.information_schema.columns c
JOIN {catalog}.information_schema.tables t
  ON t.table_catalog = c.table_catalog AND t.table_schema = c.table_schema
 AND t.table_name = c.table_name
WHERE c.table_schema <> 'information_schema' {schema_filter}
ORDER BY 2, c.ordinal_position
"""

# Snowflake has no logical-name field; teams usually keep it in a governance tag or a
# curated export table. This query builds the contract from INFORMATION_SCHEMA comments.
SNOWFLAKE_EXPORT_SQL = """
CREATE OR REPLACE TABLE {target} AS
SELECT 'SNOWFLAKE' AS PLATFORM,
       c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME AS TABLE_PHYSICAL_NAME,
       NULL::VARCHAR AS TABLE_LOGICAL_NAME, t.COMMENT AS TABLE_DESCRIPTION,
       c.COLUMN_NAME AS COLUMN_PHYSICAL_NAME, NULL::VARCHAR AS COLUMN_LOGICAL_NAME,
       c.COMMENT AS COLUMN_DESCRIPTION, c.DATA_TYPE AS COLUMN_DATATYPE,
       c.CHARACTER_MAXIMUM_LENGTH AS COLUMN_LENGTH
FROM {database}.INFORMATION_SCHEMA.COLUMNS c
JOIN {database}.INFORMATION_SCHEMA.TABLES t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA <> 'INFORMATION_SCHEMA';
"""


def _check(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return name


def databricks_rows(catalogs: list[str], *, profile: str | None = None,
                    warehouse_id: str | None = None, schemas: list[str] | None = None):
    """Yield contract rows from Unity Catalog using the Statement Execution API."""
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    if not warehouse_id:
        warehouses = [w for w in client.warehouses.list()]
        if not warehouses:
            raise ValueError("No SQL warehouse available; pass --warehouse-id.")
        warehouse_id = warehouses[0].id
    schema_filter = ""
    if schemas:
        schema_filter = "AND c.table_schema IN (" + ", ".join(
            f"'{_check(s)}'" for s in schemas) + ")"
    for catalog in catalogs:
        statement = DATABRICKS_SQL.format(catalog=f"`{_check(catalog)}`", schema_filter=schema_filter)
        response = client.statement_execution.execute_statement(
            statement=statement, warehouse_id=warehouse_id, wait_timeout="50s")
        state = response.status.state.value if response.status and response.status.state else None
        if state != "SUCCEEDED":
            message = response.status.error.message if response.status and response.status.error else state
            raise RuntimeError(f"Metadata query failed for {catalog}: {message}")
        names = [c.name for c in response.manifest.schema.columns]
        result = response.result
        while result is not None:
            for values in result.data_array or []:
                yield dict(zip(names, values))
            if result.next_chunk_index is None:
                break
            result = client.statement_execution.get_statement_result_chunk_n(
                response.statement_id, result.next_chunk_index)
