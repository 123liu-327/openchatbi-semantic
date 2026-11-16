"""Read upstream FK metadata lazily, without importing the catalog stack at import."""

from importlib import import_module
from typing import Any, Protocol, cast

from pydantic import ValidationError

from openchatbi.semantic.models import JoinEdge, JoinKey
from openchatbi.semantic.sources import SemanticSourceError


class ForeignKeyCatalog(Protocol):
    def get_table_list(self) -> list[str]: ...

    def get_sql_engine(self) -> object: ...


class ForeignKeyReader(Protocol):
    def get_foreign_keys(self, table_name: str) -> list[dict[str, Any]]: ...


class ForeignKeyJoinSource:
    def __init__(self, catalog: ForeignKeyCatalog, *, reader: ForeignKeyReader | None = None) -> None:
        self._catalog = catalog
        self._reader = reader

    def load_edges(self) -> tuple[JoinEdge, ...]:
        try:
            tables = sorted(set(self._catalog.get_table_list()))
            if not tables:
                return ()
            reader = self._reader
            if reader is None:
                # Explicit load is the I/O boundary. Dynamic import also keeps
                # the semantic type-checking boundary independent of upstream.
                factory = import_module("openchatbi.catalog.catalog_loader").DataCatalogLoader
                reader = cast(ForeignKeyReader, factory(self._catalog.get_sql_engine()))
        except Exception as exc:
            raise SemanticSourceError(f"catalog [foreign_keys] initialization: {exc}") from exc
        result = []
        for table in tables:
            qualifier, _, name = table.rpartition(".")
            try:
                records = reader.get_foreign_keys(name)
                if not isinstance(records, list):
                    raise ValueError("expected foreign key record list")
            except Exception as exc:
                raise SemanticSourceError(f"catalog [{table}] foreign_keys: {exc}") from exc
            for index, row in enumerate(records):
                try:
                    if not isinstance(row, dict):
                        raise ValueError("expected foreign key record mapping")
                    parent = row["referred_table"]
                    left, right = row["constrained_columns"], row["referred_columns"]
                    for columns in (left, right):
                        if (
                            not isinstance(columns, (list, tuple))
                            or not columns
                            or any(not isinstance(c, str) or not c for c in columns)
                        ):
                            raise ValueError("nonempty column lists required")
                    if len(left) != len(right):
                        raise ValueError("composite key lengths differ")
                    if not isinstance(parent, str) or not parent:
                        raise ValueError("referred_table required")
                    schema = row.get("referred_schema") or qualifier
                    parent = f"{schema}.{parent}" if schema else parent
                    result.append(
                        JoinEdge(
                            child=table,
                            parent=parent,
                            keys=tuple(JoinKey(left=lkey, right=rkey) for lkey, rkey in zip(left, right, strict=True)),
                            source="foreign_key",
                            provenance=f"catalog:{table}#foreign_key:{row.get('name') or index}",
                        )
                    )
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    raise SemanticSourceError(f"catalog [{table}][foreign_keys:{index}]: {exc}") from exc
        return tuple(sorted(result, key=lambda edge: edge.model_dump_json()))
