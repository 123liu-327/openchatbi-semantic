"""Exercise real Parquet-to-SQLite ingestion and idempotent repeated imports."""

import json
import sqlite3

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from scripts.import_freshretail import import_database, write_catalog  # noqa: E402 -- Optional Parquet dependency.


def test_import_preserves_arrays_grain_and_split_and_builds_dimensions(tmp_path):
    source = tmp_path / "source/data"
    source.mkdir(parents=True)
    row = {
        "city_id": 0,
        "store_id": 1,
        "product_id": 2,
        "management_group_id": 3,
        "first_category_id": 4,
        "second_category_id": 5,
        "third_category_id": 6,
        "sale_amount": 0.2,
        "hours_sale": [0.0] * 24,
        "hours_stock_status": [0] * 24,
        "stock_hour6_22_cnt": 0,
        "discount": 1.0,
        "dt": "2024-03-28",
    }
    pq.write_table(pa.Table.from_pylist([row]), source / "train.parquet")
    pq.write_table(pa.Table.from_pylist([{**row, "dt": "2024-06-26"}]), source / "eval.parquet")
    database = import_database(source.parent, tmp_path / "output")
    write_catalog(database, tmp_path / "output")
    from openchatbi.catalog.store.file_system import FileSystemCatalogStore

    catalog = FileSystemCatalogStore(str(tmp_path / "output/catalog"), {"uri": "sqlite:///" + database.as_posix()})
    assert set(catalog.get_table_list()) == {"daily_sales", "products", "stores"}
    catalog.get_sql_engine().dispose()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM daily_sales").fetchone()[0] == 2
        assert db.execute(
            "SELECT SUM(sale_amount) FROM daily_sales JOIN products USING(product_id) JOIN stores USING(store_id)"
        ).fetchone()[0] == pytest.approx(0.4)
        assert json.loads(db.execute("SELECT hours_sale FROM daily_sales LIMIT 1").fetchone()[0]) == [0.0] * 24
        assert db.execute(
            "SELECT source_split,COUNT(*) FROM daily_sales GROUP BY source_split ORDER BY source_split"
        ).fetchall() == [("eval", 1), ("train", 1)]
    assert import_database(source.parent, tmp_path / "output") == database
    # A changed input must not silently rewrite an existing business database.
    pq.write_table(pa.Table.from_pylist([{**row, "sale_amount": 1.0}]), source / "train.parquet")
    with pytest.raises(ValueError, match="Existing database"):
        import_database(source.parent, tmp_path / "output")
