"""Fail-closed SQL boundary shared by direct execution and the legacy adapter."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

_active_guard: ContextVar[bool] = ContextVar("four_domain_sql_guard", default=False)
_authorizer: ContextVar[Callable | None] = ContextVar("four_domain_sql_authorizer", default=None)


class SQLPolicyError(ValueError):
    pass


def check_sql(sql: str, dialect: str = "sqlite") -> dict:
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(sql, read={"postgresql": "postgres"}.get(dialect, dialect))
    except (sqlglot.errors.ParseError, ValueError) as exc:
        raise SQLPolicyError("SQL could not be parsed for the configured dialect") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise SQLPolicyError("Exactly one read-only SELECT query is allowed")
    statement = statements[0]
    forbidden = {
        "Insert",
        "Update",
        "Delete",
        "Create",
        "Drop",
        "Alter",
        "Merge",
        "Command",
        "Copy",
        "Into",
        "Lock",
        "Transaction",
        "Grant",
        "Revoke",
        "Execute",
        "Pragma",
        "Attach",
        "Detach",
        "Use",
        "Set",
        "LoadData",
        "TruncateTable",
    }
    for node in statement.walk():
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise SQLPolicyError("External or table-valued functions are not allowed at this SQL entry point")
        if type(node).__name__ in forbidden:
            raise SQLPolicyError("SQL contains a write, control statement, file output or locking clause")
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            # File, extension and remote table functions are not a read-only DB query.
            if name.casefold() in {
                "load_extension",
                "readfile",
                "writefile",
                "load_file",
                "dblink_exec",
                "pg_read_file",
                "pg_write_file",
                "sys_exec",
                "shell",
                "http_get",
                "dblink",
                "set_config",
                "nextval",
                "setval",
                "pg_advisory_lock",
                "pg_sleep",
                "sleep",
            }:
                raise SQLPolicyError("SQL contains a disallowed side-effect or external-access function")
    return {"allowed": True, "tables": sorted({table.name for table in statement.find_all(exp.Table)})}


def enforce_active_policy(sql: str, dialect: str):
    if _active_guard.get():
        check_sql(sql, dialect)
        authorize = _authorizer.get()
        if authorize:
            authorize(sql)


def require_authorization(sql, dialect):
    if not _active_guard.get() or _authorizer.get() is None:
        raise PermissionError("A trusted SQL authorization context is required")
    check_sql(sql, dialect)
    return _authorizer.get()(sql)


def sql_resources(sql, resolved, dialect):
    """Resolve physical column access through aliases and CTEs for authorization."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    read = {"postgresql": "postgres"}.get(dialect, dialect)
    tree = qualify(
        sqlglot.parse_one(sql, read=read),
        dialect=read,
        schema={table.table: dict.fromkeys(table.columns, "UNKNOWN") for table in resolved.tables},
    )
    resources = {}
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                resources.setdefault(".".join(part.name for part in source.parts).casefold(), set())
        for column in scope.columns:
            cursor = scope
            source = None
            while cursor is not None:
                source = cursor.sources.get(column.table)
                if source is not None:
                    break
                cursor = cursor.parent
            if isinstance(source, exp.Table):
                table = ".".join(part.name for part in source.parts).casefold()
                resources.setdefault(table, set()).add(column.name.casefold())
    return resources


def authorize_resources(sql, resolved, dialect, access):
    if not access or not access.get("principal"):
        raise PermissionError("Missing trusted user identity; configure local access or inject an identity provider")
    grants = {
        table.casefold(): {column.casefold() for column in columns}
        for table, columns in access.get("table_columns", {}).items()
    }
    resources = sql_resources(sql, resolved, dialect)
    for table, columns in resources.items():
        if table not in grants or ("*" not in grants[table] and not columns <= grants[table]):
            raise PermissionError("User is not authorized for the requested tables or fields")
    return {
        "principal": access["principal"],
        "resources": {table: sorted(columns) for table, columns in resources.items()},
    }


def check_resolved_sql(sql, resolved, dialect):
    """Validate actual SQL resources against the supplied candidate scope."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    read = {"postgresql": "postgres"}.get(dialect, dialect)
    tree = sqlglot.parse_one(sql, read=read)
    if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)):
        raise SQLPolicyError("Use explicit semantic fields instead of SELECT *")
    allowed = {table.table.casefold() for table in resolved.tables}
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                name = ".".join(part.name for part in source.parts).casefold()
                if name not in allowed:
                    raise SQLPolicyError("SQL references a table outside the resolved semantic scope")
    try:
        qualify(
            tree.copy(),
            dialect=read,
            schema={table.table: dict.fromkeys(table.columns, "UNKNOWN") for table in resolved.tables},
            validate_qualify_columns=True,
        )
    except sqlglot.errors.OptimizeError as exc:
        raise SQLPolicyError("SQL references an unknown, ambiguous or excluded semantic field") from exc


@contextmanager
def guarded_sql(authorize=None):
    token = _active_guard.set(True)
    auth_token = _authorizer.set(authorize)
    try:
        yield
    finally:
        _active_guard.reset(token)
        _authorizer.reset(auth_token)


def execute_readonly(catalog, sql: str, dialect: str, limit: int = 1000, authorize=None):
    """Public direct entry point: guard cannot be omitted by a planner."""
    from sqlalchemy import text

    check_sql(sql, dialect)
    if authorize:
        authorize(sql)
    with catalog.get_sql_engine().connect() as connection:
        result = connection.execute(text(sql))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchmany(limit)]
        # No commit; a direct read executes within a connection that rolls back.
    return columns, rows
