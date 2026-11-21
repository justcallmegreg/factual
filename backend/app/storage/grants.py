"""SQL used by the migrations for the two database roles.

The login role in DATABASE_URL owns the schema and runs the migrations. The
runtime role `factual_runtime` is what the application connects as: it can read
and insert everywhere, but it can never update or delete an evidence row. A
trigger rejects those statements for every role, the owner included.
"""

from __future__ import annotations

RUNTIME_ROLE = "factual_runtime"
REJECT_FUNCTION = "evidence_reject_change"

CREATE_RUNTIME_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        CREATE ROLE {RUNTIME_ROLE} NOLOGIN;
    END IF;
    EXECUTE format('GRANT {RUNTIME_ROLE} TO %I', current_user);
END
$$;
"""

GRANT_SCHEMA = f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};"

CREATE_REJECT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {REJECT_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence tables are insert-only: % on % is not allowed',
        TG_OP, TG_TABLE_NAME;
END
$$;
"""


def evidence_table_sql(table: str) -> list[str]:
    """Insert-only: the runtime role may read and insert; a trigger rejects the rest."""
    return [
        f"GRANT SELECT, INSERT ON {table} TO {RUNTIME_ROLE};",
        f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}"
        f" FOR EACH STATEMENT EXECUTE FUNCTION {REJECT_FUNCTION}();",
    ]


def operational_table_sql(table: str) -> list[str]:
    """Projections and operational state: the runtime role maintains them."""
    return [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RUNTIME_ROLE};"]
